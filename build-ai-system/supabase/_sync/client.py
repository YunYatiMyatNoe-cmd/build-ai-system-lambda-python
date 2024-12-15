import re
from typing import Any, Dict, List, Optional, Union

from gotrue import SyncMemoryStorage
from gotrue.types import AuthChangeEvent, Session
from httpx import Timeout
from postgrest import (
    SyncPostgrestClient,
    SyncRequestBuilder,
    SyncRPCFilterRequestBuilder,
)
from postgrest.constants import DEFAULT_POSTGREST_CLIENT_TIMEOUT
from realtime import RealtimeChannelOptions, SyncRealtimeChannel, SyncRealtimeClient
from storage3 import SyncStorageClient
from storage3.constants import DEFAULT_TIMEOUT as DEFAULT_STORAGE_CLIENT_TIMEOUT
from supafunc import SyncFunctionsClient

from ..lib.client_options import SyncClientOptions as ClientOptions
from .auth_client import SyncSupabaseAuthClient


# Create an exception class when user does not provide a valid url or key.
class SupabaseException(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class SyncClient:
    """Supabase client class."""

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        options: Optional[ClientOptions] = None,
    ):
        """Instantiate the client.

        Parameters
        ----------
        supabase_url: str
            The URL to the Supabase instance that should be connected to.
        supabase_key: str
            The API key to the Supabase instance that should be connected to.
        **options
            Any extra settings to be optionally specified - also see the
            `DEFAULT_OPTIONS` dict.
        """

        if not supabase_url:
            raise SupabaseException("supabase_url is required")
        if not supabase_key:
            raise SupabaseException("supabase_key is required")

        # Check if the url and key are valid
        if not re.match(r"^(https?)://.+", supabase_url):
            raise SupabaseException("Invalid URL")

        # Check if the key is a valid JWT
        if not re.match(
            r"^[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*$", supabase_key
        ):
            raise SupabaseException("Invalid API key")

        if options is None:
            options = ClientOptions(storage=SyncMemoryStorage())

        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.options = options
        options.headers.update(self._get_auth_headers())
        self.rest_url = f"{supabase_url}/rest/v1"
        self.realtime_url = f"{supabase_url}/realtime/v1".replace("http", "ws")
        self.auth_url = f"{supabase_url}/auth/v1"
        self.storage_url = f"{supabase_url}/storage/v1"
        self.functions_url = f"{supabase_url}/functions/v1"

        # Instantiate clients.
        self.auth = self._init_supabase_auth_client(
            auth_url=self.auth_url,
            client_options=options,
        )
        self.realtime = self._init_realtime_client(
            realtime_url=self.realtime_url,
            supabase_key=self.supabase_key,
            options=options.realtime if options else None,
        )
        self._postgrest = None
        self._storage = None
        self._functions = None
        self.auth.on_auth_state_change(self._listen_to_auth_events)

    @classmethod
    def create(
        cls,
        supabase_url: str,
        supabase_key: str,
        options: Optional[ClientOptions] = None,
    ):
        auth_header = options.headers.get("Authorization") if options else None
        client = cls(supabase_url, supabase_key, options)

        if auth_header is None:
            try:
                session = client.auth.get_session()
                session_access_token = client._create_auth_header(session.access_token)
            except Exception as err:
                session_access_token = None

            client.options.headers.update(
                client._get_auth_headers(session_access_token)
            )

        return client

    def table(self, table_name: str) -> SyncRequestBuilder:
        """Perform a table operation.

        Note that the supabase client uses the `from` method, but in Python,
        this is a reserved keyword, so we have elected to use the name `table`.
        Alternatively you can use the `.from_()` method.
        """
        return self.from_(table_name)

    def schema(self, schema: str) -> SyncPostgrestClient:
        """Select a schema to query or perform an function (rpc) call.

        The schema needs to be on the list of exposed schemas inside Supabase.
        """
        if self.options.schema != schema:
            self.options.schema = schema
            if self._postgrest:
                self._postgrest.schema(schema)
        return self.postgrest

    def from_(self, table_name: str) -> SyncRequestBuilder:
        """Perform a table operation.

        See the `table` method.
        """
        return self.postgrest.from_(table_name)

    def rpc(
        self, fn: str, params: Optional[Dict[Any, Any]] = None
    ) -> SyncRPCFilterRequestBuilder:
        """Performs a stored procedure call.

        Parameters
        ----------
        fn : callable
            The stored procedure call to be executed.
        params : dict of any
            Parameters passed into the stored procedure call.

        Returns
        -------
        SyncFilterRequestBuilder
            Returns a filter builder. This lets you apply filters on the response
            of an RPC.
        """
        if params is None:
            params = {}
        return self.postgrest.rpc(fn, params)

    @property
    def postgrest(self):
        if self._postgrest is None:
            self._postgrest = self._init_postgrest_client(
                rest_url=self.rest_url,
                headers=self.options.headers,
                schema=self.options.schema,
                timeout=self.options.postgrest_client_timeout,
            )

        return self._postgrest

    @property
    def storage(self):
        if self._storage is None:
            self._storage = self._init_storage_client(
                storage_url=self.storage_url,
                headers=self.options.headers,
                storage_client_timeout=self.options.storage_client_timeout,
            )
        return self._storage

    @property
    def functions(self):
        if self._functions is None:
            self._functions = SyncFunctionsClient(
                self.functions_url,
                self.options.headers,
                self.options.function_client_timeout,
            )
        return self._functions

    def channel(
        self, topic: str, params: RealtimeChannelOptions = {}
    ) -> SyncRealtimeChannel:
        """Creates a Realtime channel with Broadcast, Presence, and Postgres Changes."""
        return self.realtime.channel(topic, params)

    def get_channels(self) -> List[SyncRealtimeChannel]:
        """Returns all realtime channels."""
        return self.realtime.get_channels()

    def remove_channel(self, channel: SyncRealtimeChannel) -> None:
        """Unsubscribes and removes Realtime channel from Realtime client."""
        self.realtime.remove_channel(channel)

    def remove_all_channels(self) -> None:
        """Unsubscribes and removes all Realtime channels from Realtime client."""
        self.realtime.remove_all_channels()

    @staticmethod
    def _init_realtime_client(
        realtime_url: str, supabase_key: str, options: Optional[Dict[str, Any]] = None
    ) -> SyncRealtimeClient:
        if options is None:
            options = {}
        """Private method for creating an instance of the realtime-py client."""
        return SyncRealtimeClient(realtime_url, token=supabase_key, **options)

    @staticmethod
    def _init_storage_client(
        storage_url: str,
        headers: Dict[str, str],
        storage_client_timeout: int = DEFAULT_STORAGE_CLIENT_TIMEOUT,
        verify: bool = True,
        proxy: Optional[str] = None,
    ) -> SyncStorageClient:
        return SyncStorageClient(
            storage_url, headers, storage_client_timeout, verify, proxy
        )

    @staticmethod
    def _init_supabase_auth_client(
        auth_url: str,
        client_options: ClientOptions,
        verify: bool = True,
        proxy: Optional[str] = None,
    ) -> SyncSupabaseAuthClient:
        """Creates a wrapped instance of the GoTrue Client."""
        return SyncSupabaseAuthClient(
            url=auth_url,
            auto_refresh_token=client_options.auto_refresh_token,
            persist_session=client_options.persist_session,
            storage=client_options.storage,
            headers=client_options.headers,
            flow_type=client_options.flow_type,
            verify=verify,
            proxy=proxy,
        )

    @staticmethod
    def _init_postgrest_client(
        rest_url: str,
        headers: Dict[str, str],
        schema: str,
        timeout: Union[int, float, Timeout] = DEFAULT_POSTGREST_CLIENT_TIMEOUT,
        verify: bool = True,
        proxy: Optional[str] = None,
    ) -> SyncPostgrestClient:
        """Private helper for creating an instance of the Postgrest client."""
        return SyncPostgrestClient(
            rest_url,
            headers=headers,
            schema=schema,
            timeout=timeout,
            verify=verify,
            proxy=proxy,
        )

    def _create_auth_header(self, token: str):
        return f"Bearer {token}"

    def _get_auth_headers(self, authorization: Optional[str] = None) -> Dict[str, str]:
        if authorization is None:
            authorization = self.options.headers.get(
                "Authorization", self._create_auth_header(self.supabase_key)
            )

        """Helper method to get auth headers."""
        return {
            "apiKey": self.supabase_key,
            "Authorization": authorization,
        }

    def _listen_to_auth_events(
        self, event: AuthChangeEvent, session: Optional[Session]
    ):
        access_token = self.supabase_key
        if event in ["SIGNED_IN", "TOKEN_REFRESHED", "SIGNED_OUT"]:
            # reset postgrest and storage instance on event change
            self._postgrest = None
            self._storage = None
            self._functions = None
            access_token = session.access_token if session else self.supabase_key

        self.options.headers["Authorization"] = self._create_auth_header(access_token)


def create_client(
    supabase_url: str,
    supabase_key: str,
    options: Optional[ClientOptions] = None,
) -> SyncClient:
    """Create client function to instantiate supabase client like JS runtime.

    Parameters
    ----------
    supabase_url: str
        The URL to the Supabase instance that should be connected to.
    supabase_key: str
        The API key to the Supabase instance that should be connected to.
    **options
        Any extra settings to be optionally specified - also see the
        `DEFAULT_OPTIONS` dict.

    Examples
    --------
    Instantiating the client.
    >>> import os
    >>> from supabase import create_client, Client
    >>>
    >>> url: str = os.environ.get("SUPABASE_TEST_URL")
    >>> key: str = os.environ.get("SUPABASE_TEST_KEY")
    >>> supabase: Client = create_client(url, key)

    Returns
    -------
    Client
    """
    return SyncClient.create(
        supabase_url=supabase_url, supabase_key=supabase_key, options=options
    )