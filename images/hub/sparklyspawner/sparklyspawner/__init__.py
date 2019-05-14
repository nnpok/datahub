from kubespawner import KubeSpawner
from traitlets import Unicode
from google.cloud import storage
from google.api_core.exceptions import Conflict
from google.api_core.iam import Policy
from googleapiclient.errors import HttpError
import tempfile
from google.oauth2 import service_account
import googleapiclient.discovery
import json
import base64

class SparklySpawner(KubeSpawner):
    gcp_service_key = Unicode(
        None,
        allow_none=True,
        config=True,
        help="""
        Google Service Account JSON key for authenticating to GCP.

        *Must* be set for SparklySpawner to work
        """
    )

    gcp_project = Unicode(
        None,
        allow_none=True,
        config=True,
        help="""
        Name of Google Cloud Project this hub is running in.

        *Must* be set.
        """
    )

    storage_bucket_template = Unicode(
        None,
        allow_none=True,
        config=True,
        help="""
        Template to use when creating storage buckets for users.

        {username} is expanded.
        """
    )

    service_account_template = Unicode(
        None,
        allow_none=True,
        config=True,
        help="""
        Template to use when creating service accounts for users.

        {username} is expanded.
        """
    )

    async def ensure_gcp_resources(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(self.gcp_service_key.encode())
            f.flush()
            storage_client = storage.Client.from_service_account_json(f.name)

            credentials = service_account.Credentials.from_service_account_file(
                f.name, scopes=['https://www.googleapis.com/auth/cloud-platform'])
            service = googleapiclient.discovery.build('iam', 'v1', credentials=credentials)


        # Create bucket if it doesn't exist
        # FIXME: Don't use a private method?
        bucket_name = self._expand_all(self.storage_bucket_template)

        bucket = storage.Bucket(storage_client, bucket_name)

        try:
            bucket.create()
            self.log.info(f'Creating {bucket_name}')
        except Conflict as e:
            # Bucket already exists
            self.log.info(f'Not creating {bucket_name}, it already exists')
        self.environment['SPARK_GCS_BUCKET'] = bucket_name

        # This is how service account emails are formatted
        # FIXME: Clip this to 30char
        sa_name = self._expand_all(self.service_account_template)
        sa_email = f'{sa_name}@{self.gcp_project}.iam.gserviceaccount.com'
        try:
            sa = service.projects().serviceAccounts().create(
                name=f'projects/{self.gcp_project}',
                body={
                    'accountId': sa_name,
                    'serviceAccount': {'displayName': sa_name}
            }).execute()
            # We assume this create call will create a service account with email sa_email
            assert sa_email == sa['email']            
            self.log.info(f'Created service account {sa["email"]}')
        except HttpError as e:
            if e.resp.status == 409:
                self.log.info(f'Created service account {sa_email}')
            else:
                raise

        # Grant SA access to bucket if it isn't present
        policy = bucket.get_iam_policy()
        role = 'roles/storage.objectAdmin'
        if Policy.service_account(sa_email) not in policy.get(role, set()):
            policy[role].add(Policy.service_account(sa_email))
            bucket.set_iam_policy(policy)

        # Check if _key exists in bucket. This is where we store private key
        key_blob = bucket.blob('__key__.json')
        if not key_blob.exists():
            key = service.projects().serviceAccounts().keys().create(
                name=f'projects/{self.gcp_project}/serviceAccounts/{sa_email}', body={}
            ).execute()
        else:
            key = json.loads(key_blob.download_as_string())

        self.environment['SPARK_GCS_KEY'] = base64.b64decode(key['privateKeyData']).decode()

    async def start(self):
        self.log.info('Testing Sparkly spawner')
        await self.ensure_gcp_resources()
        return await super().start()