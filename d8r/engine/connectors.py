"""Explicit, credential-safe Ibis connections and namespace-scoped discovery.

Profiles contain JSON data, never import paths, Python expressions or driver objects.
Optional drivers are imported only after validation, when the user connects. Query
compilation and execution remain Ibis's responsibility on every backend.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import ibis
import ibis.expr.operations as ops
import sqlglot as sg

from d8r.query import relation_name

from .datasources import DataSource


@dataclass(frozen=True)
class ConnectionField:
    name: str
    label: str
    default: str = ""
    secret: bool = False
    required: bool = False


@dataclass(frozen=True)
class BackendSpec:
    label: str
    fields: tuple[ConnectionField, ...]
    help: str
    extra: str


class BackendConnectionError(ValueError):
    """An intentionally public error; never contains driver/server exception text."""


def _login(port: str, user: str = "") -> tuple[ConnectionField, ...]:
    return (
        ConnectionField("host", "Host", "localhost", required=True),
        ConnectionField("port", "Port", port, required=True),
        ConnectionField("user", "User", user),
        ConnectionField("password", "Password", secret=True),
    )


BACKENDS: dict[str, BackendSpec] = {
    "duckdb": BackendSpec("DuckDB", (
        ConnectionField("database", "Database file", ":memory:", required=True),
        ConnectionField("schema", "Schema", "main", required=True),
        ConnectionField("read_only", "Read only (true/false)", "false"),
    ), "Embedded DuckDB. Use an existing database file or :memory:. Advanced options are DuckDB settings.", "duckdb"),
    "athena": BackendSpec("Amazon Athena", (
        ConnectionField("s3_staging_dir", "S3 query results directory", required=True),
        ConnectionField("region_name", "AWS region", required=True),
        ConnectionField("catalog_name", "Catalog", "awsdatacatalog", required=True),
        ConnectionField("schema_name", "Database", "default", required=True),
        ConnectionField("aws_access_key_id", "AWS access key ID", secret=True),
        ConnectionField("aws_secret_access_key", "AWS secret access key", secret=True),
        ConnectionField("aws_session_token", "AWS session token", secret=True),
    ), "Requires PyAthena and S3 access; blank credentials use the AWS credential chain. Advanced options accept work_group/profile_name and PyAthena kwargs.", "athena"),
    "bigquery": BackendSpec("BigQuery", (
        ConnectionField("project_id", "Billing project", required=True),
        ConnectionField("dataset_id", "Dataset (or project.dataset)", required=True),
        ConnectionField("location", "Location"),
        ConnectionField("credentials_file", "Service account JSON file", secret=True),
    ), "Uses Google Application Default Credentials, or a service-account JSON file. Advanced credentials may contain a service-account JSON object. No interactive OAuth browser is started.", "bigquery"),
    "clickhouse": BackendSpec("ClickHouse", _login("8443", "default") + (
        ConnectionField("database", "Database", "default", required=True),
        ConnectionField("secure", "HTTPS (true/false)", "true"),
    ), "Uses clickhouse-connect's HTTP interface, not its native TCP port. Advanced settings and TLS options are passed to the driver.", "clickhouse"),
    "databricks": BackendSpec("Databricks", (
        ConnectionField("server_hostname", "Server hostname", required=True),
        ConnectionField("http_path", "SQL warehouse HTTP path", required=True),
        ConnectionField("access_token", "Access token", secret=True),
        ConnectionField("catalog", "Catalog", "main", required=True),
        ConnectionField("schema", "Schema", "default", required=True),
    ), "Requires a running Databricks SQL warehouse and databricks-sql-connector. Advanced options accept connector authentication/session settings, not Python credential-provider objects.", "databricks"),
    "datafusion": BackendSpec("DataFusion", (
        ConnectionField("catalog", "Catalog", "datafusion", required=True),
        ConnectionField("schema", "Schema", "public", required=True),
    ), 'Creates a real native SessionContext. Advanced config is a string-valued SessionConfig object; tables maps names to local CSV/Parquet paths. Requires a compatible DataFusion native wheel.', "datafusion"),
    "druid": BackendSpec("Apache Druid", _login("8082") + (
        ConnectionField("path", "SQL HTTP path", "/druid/v2/sql/", required=True),
        ConnectionField("scheme", "HTTP scheme", "https", required=True),
        ConnectionField("schema", "Schema", "druid", required=True),
    ), "Uses PyDruid SQL HTTP. Advanced options are PyDruid connection kwargs. Discovery reads INFORMATION_SCHEMA only.", "druid"),
    "exasol": BackendSpec("Exasol", _login("8563", "sys") + (
        ConnectionField("schema", "Schema", required=True),
    ), "Requires PyExasol/WebSocket access. TLS certificate verification is enabled; advanced websocket_sslopt may supply CA settings.", "exasol"),
    "flink": BackendSpec("Apache Flink", (
        ConnectionField("catalog", "Catalog", "default_catalog", required=True),
        ConnectionField("database", "Database", "default_database", required=True),
    ), 'Requires separately installed apache-flink/PyFlink and a compatible Java runtime (not bundled; native Windows/Python versions may be unsupported). Creates a batch TableEnvironment. Advanced config holds Flink settings; catalogs maps names to catalog property objects. Session-local tables map names to connector properties plus schema={"column":"BIGINT"} using Flink SQL types.', "flink"),
    "impala": BackendSpec("Impala", _login("21050") + (
        ConnectionField("database", "Database", "default", required=True),
        ConnectionField("auth_mechanism", "Auth (NOSASL/PLAIN/GSSAPI/LDAP)", "NOSASL"),
        ConnectionField("use_ssl", "TLS (true/false)", "true"),
    ), "Requires impyla and a reachable HiveServer2 endpoint. Kerberos/GSSAPI may require additional native SASL/Kerberos libraries.", "impala"),
    "materialize": BackendSpec("Materialize", _login("6875", "materialize") + (
        ConnectionField("database", "Database", "materialize", required=True),
        ConnectionField("schema", "Schema", "public", required=True),
        ConnectionField("sslmode", "SSL mode", "require"),
        ConnectionField("cluster", "Cluster"),
    ), "Uses Ibis's real Materialize backend and psycopg. Advanced kwargs configure libpq. No PostgreSQL transaction capabilities are assumed.", "materialize"),
    "mssql": BackendSpec("SQL Server", _login("1433") + (
        ConnectionField("database", "Database", required=True),
        ConnectionField("schema", "Schema", "dbo", required=True),
        ConnectionField("driver", "Installed ODBC driver", "ODBC Driver 18 for SQL Server", required=True),
    ), "Requires pyodbc plus a separately installed SQL Server ODBC driver. Blank user/password uses integrated authentication. Advanced options include Encrypt and TrustServerCertificate.", "mssql"),
    "mysql": BackendSpec("MySQL", _login("3306") + (
        ConnectionField("database", "Database", required=True),
    ), "Uses mysqlclient (MySQLdb); some platforms require native MySQL/MariaDB development libraries. Advanced ssl is a driver TLS-options object.", "mysql"),
    "oracle": BackendSpec("Oracle", _login("1521") + (
        ConnectionField("service_name", "Service name"),
        ConnectionField("schema", "Schema (blank = current user)"),
    ), "Uses python-oracledb thin mode. Specify service_name, or advanced sid/dsn. Advanced options go to oracledb.connect; no arbitrary client-library initialization is performed.", "oracle"),
    "pyspark": BackendSpec("PySpark", (
        ConnectionField("master", "Spark master", "local[*]", required=True),
        ConnectionField("app_name", "Application name", "D8R", required=True),
        ConnectionField("catalog", "Catalog", "spark_catalog", required=True),
        ConnectionField("database", "Database", "default", required=True),
    ), 'Requires PySpark and a compatible Java runtime. Creates an owned batch SparkSession; advanced config holds Spark settings, enable_hive_support enables Hive, and remote selects Spark Connect instead of master.', "pyspark"),
    "risingwave": BackendSpec("RisingWave", _login("4566", "root") + (
        ConnectionField("database", "Database", "dev", required=True),
        ConnectionField("schema", "Schema", "public", required=True),
        ConnectionField("sslmode", "SSL mode", "prefer"),
    ), "Uses Ibis RisingWave and psycopg2. Advanced options configure libpq, including TLS and connect_timeout. psycopg2 may need libpq/build tools.", "risingwave"),
    "snowflake": BackendSpec("Snowflake", (
        ConnectionField("account", "Account identifier", required=True),
        ConnectionField("user", "User", required=True),
        ConnectionField("password", "Password", secret=True),
        ConnectionField("database", "Database", required=True),
        ConnectionField("schema", "Schema", "PUBLIC", required=True),
        ConnectionField("warehouse", "Warehouse", required=True),
        ConnectionField("role", "Role"),
    ), "Uses snowflake-connector-python. Advanced options accept authenticator, token, private_key_file and connector kwargs. Ibis object-UDF installation is disabled by default.", "snowflake"),
    "sqlite": BackendSpec("SQLite", (
        ConnectionField("database", "Database file", required=True),
        ConnectionField("schema", "Schema", "main", required=True),
    ), "Opens an existing SQLite file, or :memory:, through Ibis. This is separate from Cloudflare D1. Advanced timeout/uri/type_map are supported.", "sqlite"),
    "trino": BackendSpec("Trino", _login("443") + (
        ConnectionField("catalog", "Catalog", required=True),
        ConnectionField("schema", "Schema", "default", required=True),
        ConnectionField("http_scheme", "HTTP scheme", "https", required=True),
    ), 'Uses the real Ibis/Trino driver. Password uses BasicAuthentication over HTTPS. Advanced auth={"type":"jwt","token":"…"} enables JWT; verify accepts true/false or a CA file. Other JSON kwargs pass to trino.dbapi.connect (e.g. session_properties, roles, request_timeout). HTTP is allowed only without credentials.', "trino"),
}


_SECRET_KEY = re.compile(
    r"password|passwd|passphrase|secret|token|credential|authorization|authentication|"
    r"(?:^|[_.-])auth(?:$|[_.-])|(?:^|[_.-])(?:api|access|private|client|account)[_.-]?key|"
    r"^(?:pwd|sslkey|key|keyfile|headers|http_headers|cookies?|sas|session_id|extra_credential.*)$",
    re.IGNORECASE,
)
_IDENTITY_USERS = {"user", "username", "uid", "login", "principal", "client_email"}
_SPARK_LOCK = Lock()


def _secret_key(key: str) -> bool:
    normalized = re.sub(r"([a-z])([A-Z])", r"\1_\2", key)
    return bool(_SECRET_KEY.search(normalized))


def _url_secrets(value: str) -> list[str]:
    if "://" not in value:
        return []
    try:
        url = urlsplit(value)
        secrets = [part for value in (url.username, url.password) if value for part in (value, unquote(value))]
        secrets.extend(val for key, val in parse_qsl(url.query) if _secret_key(key) and val)
        return secrets
    except ValueError:
        return [value]


def profile_secrets(profile: dict) -> list[str]:
    """Collect nested secrets without loading any optional backend or driver."""
    found: set[str] = set()
    backend = profile.get("backend") if isinstance(profile, dict) else None
    spec = BACKENDS.get(backend) if isinstance(backend, str) else None
    secret_fields = {field.name for field in spec.fields if field.secret} if spec else set()

    def visit(value, secret: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, secret or _secret_key(str(key)) or key in secret_fields)
        elif isinstance(value, list):
            for item in value:
                visit(item, secret)
        elif isinstance(value, str):
            if secret and value:
                found.add(value)
                if value.lower().startswith("bearer ") and value[7:].strip():
                    found.add(value[7:].strip())
            found.update(_url_secrets(value))
        elif secret and value is not None:
            found.add(str(value))

    if isinstance(profile, dict):
        visit(profile.get("options", {}))
    return sorted(found, key=lambda value: (-len(value), value))


def _public(value: str, secrets: list[str]) -> str:
    for secret in secrets:
        value = value.replace(secret, "[redacted]")
    return " ".join(value.split())


def _json_copy(value, depth: int = 0):
    if depth > 32:
        raise BackendConnectionError("Connection options are nested too deeply")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, str) and "\x00" not in value:
        return value
    if isinstance(value, list):
        return [_json_copy(item, depth + 1) for item in value]
    if isinstance(value, dict) and all(
        isinstance(key, str) and key and not any(ord(char) < 32 for char in key)
        for key in value
    ):
        return {key: _json_copy(item, depth + 1) for key, item in value.items()}
    raise BackendConnectionError("Connection options must contain valid JSON values")


def _boolean(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise BackendConnectionError("Boolean connection options must be true or false")


def validate_profile(profile: dict) -> dict:
    """Normalize a profile structurally, with no driver import or network access."""
    if not isinstance(profile, dict) or not isinstance(profile.get("backend"), str):
        raise BackendConnectionError("Select a supported database backend")
    backend = profile["backend"]
    spec = BACKENDS.get(backend)
    if spec is None:
        raise BackendConnectionError("Select a supported database backend")
    if profile.get("kind", "ibis") != "ibis" or set(profile) - {"kind", "backend", "options", "display"}:
        raise BackendConnectionError("Invalid database connection profile")
    options = _json_copy(profile.get("options", {}))
    if not isinstance(options, dict):
        raise BackendConnectionError("Connection options must be a JSON object")
    for field in spec.fields:
        value = options.get(field.name, field.default)
        if isinstance(value, str) and not field.secret:
            value = value.strip()
        if value == "" or value is None:
            if field.required:
                raise BackendConnectionError(f"{spec.label}: {field.label} is required")
            options.pop(field.name, None)
        else:
            if field.name not in {"port", "read_only", "secure", "use_ssl"} and not isinstance(value, str):
                raise BackendConnectionError(f"{spec.label}: {field.label} must be text")
            if isinstance(value, str) and not field.secret and any(ord(char) < 32 for char in value):
                raise BackendConnectionError(f"{spec.label}: {field.label} contains invalid characters")
            options[field.name] = value
    if "port" in options:
        value = options["port"]
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]{1,5}", str(value)) or not 1 <= int(value) <= 65535:
            raise BackendConnectionError("Port must be an integer from 1 to 65535")
        options["port"] = int(value)
    for key in ("read_only", "secure", "use_ssl", "autocommit", "use_cloud_fetch", "create_object_udfs", "enable_hive_support", "uri"):
        if key in options:
            options[key] = _boolean(options[key])
    for key in ("host", "server_hostname"):
        if key in options and (not isinstance(options[key], str) or any(char in options[key] for char in ("/", "@", "?", "#"))):
            raise BackendConnectionError("Host must be a hostname or address, not a URL or credentials")
    for key in ("http_scheme", "scheme"):
        if key in options and (not isinstance(options[key], str) or options[key] not in {"http", "https"}):
            raise BackendConnectionError("HTTP scheme must be http or https")
    if "sslmode" in options and (not isinstance(options["sslmode"], str) or options["sslmode"] not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}):
        raise BackendConnectionError("Select a supported SSL mode")
    if set(options) & {"client", "storage_client", "http_session", "session", "table_env", "cursor_class", "credentials_provider", "generate_job_id_prefix"}:
        raise BackendConnectionError("Connection profiles accept JSON options, not Python runtime objects or providers")
    for key in ("config", "tables", "catalogs"):
        if backend in {"datafusion", "flink", "pyspark"} and key in options and not isinstance(options[key], dict):
            raise BackendConnectionError("Runtime config, tables and catalogs must be JSON objects")
    if backend == "trino":
        if "database" in options:
            raise BackendConnectionError("Trino uses the catalog field; do not also supply database")
        auth = options.get("auth")
        if auth is not None:
            if options.get("password"):
                raise BackendConnectionError("Choose either Trino password or advanced auth, not both")
            if not isinstance(auth, dict) or not isinstance(auth.get("type"), str) or auth["type"] not in {"basic", "jwt"}:
                raise BackendConnectionError("Trino auth must be an object with type basic or jwt")
            expected = {"type", "token"} if auth["type"] == "jwt" else {"type", "username", "password"}
            if set(auth) != expected or any(not isinstance(auth[key], str) or not auth[key] for key in expected):
                raise BackendConnectionError("Trino auth requires a JWT token, or a Basic username and password")
        if options["http_scheme"] != "https" and profile_secrets({"backend": backend, "options": options}):
            raise BackendConnectionError("Trino credentials, including advanced authentication headers, require HTTPS")
        if not options.get("user"):
            raise BackendConnectionError("Trino: User is required")
        if "verify" in options and not isinstance(options["verify"], (bool, str)):
            raise BackendConnectionError("Trino verify must be true, false or a CA certificate file")
    if backend == "bigquery" and "credentials" in options:
        if not isinstance(options["credentials"], dict) or "credentials_file" in options:
            raise BackendConnectionError("Choose one service-account JSON object or credentials file")
    if backend == "oracle":
        if not options.get("user"):
            raise BackendConnectionError("Oracle: User is required")
        if sum(bool(options.get(key)) for key in ("dsn", "sid", "service_name", "database")) != 1:
            raise BackendConnectionError("Oracle requires exactly one service name, SID or DSN")
    if backend in {"exasol", "mysql"} and not options.get("user"):
        raise BackendConnectionError(f"{spec.label}: User is required")
    if backend in {"datafusion", "flink", "pyspark"}:
        allowed = {field.name for field in spec.fields} | {
            "datafusion": {"config", "tables"},
            "flink": {"config", "tables", "catalogs"},
            "pyspark": {"config", "enable_hive_support", "remote"},
        }[backend]
        if set(options) - allowed:
            raise BackendConnectionError(f"{spec.label}: put runtime settings in the advanced config object")
        config = options.get("config", {})
        if not all(isinstance(value, (str, int, float, bool)) for value in config.values()):
            raise BackendConnectionError("Runtime config values must be strings, numbers or booleans")
        if backend == "datafusion":
            if not all(isinstance(value, str) and Path(value).suffix.lower() in {".csv", ".parquet", ".pq"} for value in options.get("tables", {}).values()):
                raise BackendConnectionError("DataFusion tables must map names to CSV/Parquet paths")
        if backend == "flink":
            for properties in options.get("catalogs", {}).values():
                if not isinstance(properties, dict) or not all(isinstance(value, (str, int, float, bool)) for value in properties.values()):
                    raise BackendConnectionError("Flink catalogs must map names to property objects")
            for properties in options.get("tables", {}).values():
                if not isinstance(properties, dict) or not isinstance(properties.get("connector"), str) or not isinstance(properties.get("schema"), dict):
                    raise BackendConnectionError("Flink tables require connector and schema objects; schema maps column names to Flink SQL types")
                if not all(isinstance(value, str) for value in properties["schema"].values()):
                    raise BackendConnectionError("Flink column types must be strings")
                if not all(isinstance(value, (str, int, float, bool)) for key, value in properties.items() if key != "schema"):
                    raise BackendConnectionError("Flink connector properties must be strings, numbers or booleans")
        if backend == "pyspark" and "remote" in options and not isinstance(options["remote"], str):
            raise BackendConnectionError("Spark Connect remote must be a URL string")
    display = profile.get("display", "")
    if not isinstance(display, str):
        raise BackendConnectionError("Connection display name must be text")
    normalized = {"backend": backend, "options": options, "display": display}
    normalized["display"] = _public(display.strip() or spec.label, profile_secrets(normalized))
    return normalized


def _identity_value(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not _secret_key(key):
                result[key] = _identity_value(item)
            elif isinstance(item, dict):
                # A principal changes visible data/permissions; password/token
                # rotation does not. Preserve principals even inside auth JSON.
                users = {name: user for name, user in item.items() if name.lower() in _IDENTITY_USERS}
                if users:
                    result[key] = users
        return result
    if isinstance(value, list):
        return [_identity_value(item) for item in value]
    if isinstance(value, str) and "://" in value:
        try:
            url = urlsplit(value)
            host = url.netloc.rsplit("@", 1)[-1]
            if url.username is not None:
                host = url.username + "@" + host
            return urlunsplit((url.scheme, host, url.path,
                              urlencode([(key, val) for key, val in parse_qsl(url.query)
                                         if not _secret_key(key)]), url.fragment))
        except ValueError:
            return "[credential URL]"
    return value


def profile_id(profile: dict) -> str:
    """Stable opaque target identity: secrets/display excluded, principals retained."""
    normalized = validate_profile(profile)
    identity = {"backend": normalized["backend"], "options": _identity_value(normalized["options"])}
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def close_backend_connection(con) -> None:
    """Release a generic source's owned handles, including auxiliary runtimes."""
    if con is None:
        return
    if getattr(con, "name", None) == "flink":
        # Ibis disconnect is a no-op. Close only this environment's catalogs,
        # never the shared process-wide PyFlink gateway.
        environment = getattr(con, "_table_env", None)
        if environment is not None:
            try:
                for name in environment.list_catalogs():
                    catalog = environment.get_catalog(name)
                    if catalog is not None:
                        try:
                            catalog._j_catalog.close()
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                con._table_env = None
    # BigQuery's Ibis disconnect only closes the REST client, not its gRPC client.
    storage = getattr(con, "storage_client", None)
    if storage is not None:
        try:
            storage.close()
        except Exception:
            pass
        con.storage_client = None
    try:
        con.disconnect()
    except Exception:
        raw = getattr(con, "con", None)
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass
    if getattr(con, "name", None) == "datafusion":
        # SessionContext has no close API; release the native context reference.
        con.con = None


def _setting(value) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _configure(con, backend: str, options: dict) -> None:
    """Adapt JSON to the concrete installed Ibis 12 driver/runtime APIs."""
    kwargs = dict(options)
    if backend == "trino":
        from trino.auth import BasicAuthentication, JWTAuthentication

        kwargs["database"] = kwargs.pop("catalog")
        password = kwargs.pop("password", None)
        auth = kwargs.pop("auth", None)
        if password:
            kwargs["auth"] = BasicAuthentication(kwargs["user"], password)
        elif auth:
            kwargs["auth"] = JWTAuthentication(auth["token"]) if auth["type"] == "jwt" else BasicAuthentication(auth["username"], auth["password"])
        kwargs.setdefault("request_timeout", 10)
        kwargs.setdefault("max_attempts", 1)
        if isinstance(kwargs["request_timeout"], list):
            kwargs["request_timeout"] = tuple(kwargs["request_timeout"])
    elif backend == "sqlite":
        import sqlite3

        database = kwargs.pop("database")
        kwargs.pop("schema", None)
        type_map = kwargs.pop("type_map", None)
        uri = kwargs.pop("uri", False)
        if database != ":memory:" and not uri and not Path(database).expanduser().is_file():
            raise BackendConnectionError("SQLite database file does not exist")
        if not uri:
            database = str(Path(database).expanduser()) if database != ":memory:" else database
        con.con = sqlite3.connect(database, check_same_thread=False, isolation_level=None, uri=uri, **kwargs)
        con._post_connect(type_map)
        return
    elif backend == "bigquery":
        import google.auth
        from google.oauth2 import service_account

        credentials = kwargs.pop("credentials", None)
        path = kwargs.pop("credentials_file", None)
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        if credentials is not None:
            kwargs["credentials"] = service_account.Credentials.from_service_account_info(credentials, scopes=scopes)
        elif path:
            kwargs["credentials"] = service_account.Credentials.from_service_account_file(str(Path(path).expanduser()), scopes=scopes)
        else:
            kwargs["credentials"], _ = google.auth.default(scopes=scopes)
    elif backend == "datafusion":
        from datafusion import SessionConfig, SessionContext

        settings = {"datafusion.sql_parser.dialect": "PostgreSQL", **{key: _setting(value) for key, value in kwargs.get("config", {}).items()}}
        settings["datafusion.catalog.default_catalog"] = kwargs["catalog"]
        settings["datafusion.catalog.default_schema"] = kwargs["schema"]
        ctx = SessionContext(SessionConfig(settings).with_information_schema(True))
        con.do_connect(ctx)
        for name, path in kwargs.get("tables", {}).items():
            if not isinstance(path, str) or not isinstance(name, str):
                raise BackendConnectionError("DataFusion tables must map names to CSV/Parquet paths")
            suffix = Path(path).suffix.lower()
            if suffix in {".parquet", ".pq"}:
                con.read_parquet(path, table_name=name)
            elif suffix == ".csv":
                con.read_csv(path, table_name=name)
            else:
                raise BackendConnectionError("DataFusion table files must be CSV or Parquet")
        return
    elif backend == "pyspark":
        from pyspark.sql import SparkSession

        # Serialize getOrCreate so two connection workers cannot both acquire
        # ownership of one context. Never adopt/stop another source's session.
        with _SPARK_LOCK:
            if SparkSession.getActiveSession() is not None or SparkSession._instantiatedSession is not None:
                raise BackendConnectionError("A Spark session is already active; disconnect it before opening another")
            builder = SparkSession.builder.appName(kwargs["app_name"])
            if remote := kwargs.get("remote"):
                builder = builder.remote(remote)
            else:
                builder = builder.master(kwargs["master"])
            for key, value in kwargs.get("config", {}).items():
                builder = builder.config(key, value)
            if kwargs.get("enable_hive_support"):
                builder = builder.enableHiveSupport()
            session = builder.getOrCreate()
            con._session = session  # own it before Ibis initialization can fail
        con.do_connect(session, mode="batch")
        session.catalog.setCurrentCatalog(kwargs["catalog"])
        session.catalog.setCurrentDatabase(kwargs["database"])
        return
    elif backend == "flink":
        from pyflink.common import Configuration
        from pyflink.table import EnvironmentSettings, Schema, TableDescriptor, TableEnvironment
        from pyflink.table.catalog import CatalogDescriptor

        configuration = Configuration()
        for key, value in kwargs.get("config", {}).items():
            configuration.set_string(key, _setting(value))
        settings = EnvironmentSettings.new_instance().in_batch_mode().with_configuration(configuration).build()
        table_env = TableEnvironment.create(settings)
        con.do_connect(table_env)
        for name, properties in kwargs.get("catalogs", {}).items():
            config = Configuration()
            for key, value in properties.items():
                config.set_string(key, _setting(value))
            table_env.create_catalog(name, CatalogDescriptor.of(name, config))
        table_env.use_catalog(kwargs["catalog"])
        table_env.use_database(kwargs["database"])
        for name, properties in kwargs.get("tables", {}).items():
            properties = dict(properties)
            connector = properties.pop("connector")
            schema = Schema.new_builder()
            for column, dtype in properties.pop("schema").items():
                schema.column(column, dtype)
            descriptor = TableDescriptor.for_connector(connector).schema(schema.build())
            for key, value in properties.items():
                descriptor.option(key, _setting(value))
            # Table registration is session-local; never mutate an external catalog.
            path = sg.to_identifier(name, quoted=True).sql(dialect=con.dialect)
            table_env.create_temporary_table(path, descriptor.build())
        return
    elif backend == "risingwave":
        import psycopg2

        kwargs.pop("schema", None)
        kwargs.setdefault("connect_timeout", 10)
        con.con = psycopg2.connect(**kwargs)
        con._post_connect()
        with con.begin() as cursor:
            cursor.execute("SET RW_IMPLICIT_FLUSH TO true")
        return
    elif backend == "oracle":
        import oracledb

        kwargs.pop("schema", None)
        host, port = kwargs.pop("host"), kwargs.pop("port")
        service = kwargs.pop("service_name", kwargs.pop("database", None))
        sid = kwargs.pop("sid", None)
        dsn = kwargs.pop("dsn", None) or oracledb.makedsn(host, port, service_name=service, sid=sid)
        kwargs.setdefault("password", "")
        kwargs.setdefault("stmtcachesize", 0)
        con.con = oracledb.connect(dsn=dsn, **kwargs)
        con._post_connect()
        return
    elif backend == "clickhouse":
        # Ibis 12 consumes secure but does not forward it to clickhouse-connect.
        # Its interface kwarg does get forwarded, and controls real transport.
        kwargs["interface"] = "https" if kwargs.pop("secure") else "http"
        kwargs.setdefault("connect_timeout", 10)
    elif backend == "exasol":
        kwargs.setdefault("password", "")
        kwargs.setdefault("websocket_sslopt", {"cert_reqs": ssl.CERT_REQUIRED})
    elif backend == "snowflake":
        kwargs.setdefault("create_object_udfs", False)
        kwargs.setdefault("login_timeout", 10)
    elif backend in {"duckdb", "mssql", "druid"}:
        kwargs.pop("schema", None)
    elif backend == "materialize":
        # Ibis interpolates cluster into a string literal; quote with the driver.
        cluster = kwargs.pop("cluster", None)
        kwargs.pop("schema", None)
        kwargs.setdefault("connect_timeout", 10)
        con.do_connect(**kwargs)
        if cluster:
            from psycopg import sql

            with con.con.cursor() as cursor:
                cursor.execute(sql.SQL("SET cluster = {}").format(sql.Literal(cluster)))
        return
    con.do_connect(**kwargs)


def _namespace(con, backend: str, options: dict) -> tuple[str | None, str]:
    if backend == "athena":
        return options["catalog_name"], options["schema_name"]
    if backend == "bigquery":
        return con.data_project, con.dataset
    if backend in {"trino", "databricks", "datafusion"}:
        return options["catalog"], options["schema"]
    if backend in {"flink", "pyspark"}:
        return options["catalog"], options["database"]
    if backend in {"materialize", "mssql", "risingwave", "snowflake"}:
        return options["database"], options["schema"]
    if backend in {"clickhouse", "mysql", "impala"}:
        return None, options["database"]
    if backend == "duckdb":
        return con.current_catalog, options["schema"]
    if backend == "oracle":
        return None, options.get("schema") or con.current_database
    return None, options["schema"]


def _relations(con, backend: str, catalog: str | None, schema: str):
    """Names and exact Ibis table kwargs; metadata only, never table row counts."""
    database = (catalog, schema) if catalog is not None else (schema,)
    table_kwargs = {"database": database}
    if backend == "flink":
        table_kwargs = {"catalog": catalog, "database": schema}
        names = con.list_tables(**table_kwargs) + con.list_tables(temp=True)
    elif backend == "datafusion":
        # Ibis list_tables only filters schema, not catalog; use its native
        # catalog API instead (same API used by Ibis get_schema).
        cat = con.con.catalog(catalog)
        native_schema = cat.schema(schema) if hasattr(cat, "schema") else cat.database(schema)
        names = native_schema.names()
    elif backend == "druid":
        # Ibis explicitly rejects list_tables(database=...). Scope its metadata
        # relation through Ibis, rather than silently listing every namespace.
        metadata = ops.DatabaseTable(
            "TABLES", ibis.schema({"TABLE_SCHEMA": "string", "TABLE_NAME": "string"}),
            source=con, namespace=ops.Namespace(database="INFORMATION_SCHEMA"),
        ).to_expr()
        names = metadata.filter(metadata.TABLE_SCHEMA == schema).select("TABLE_NAME").execute().TABLE_NAME.tolist()
    elif backend in {"sqlite", "clickhouse", "exasol"}:
        names = con.list_tables(database=schema)
    elif backend == "impala":
        # This backend inserts database verbatim in SHOW TABLES. Quote with its
        # own SQL dialect; table() still receives the original identifier tuple.
        names = con.list_tables(database=sg.to_identifier(schema, quoted=True).sql(dialect=con.dialect))
    elif backend == "pyspark":
        # Ibis inserts the db identifier unquoted. The runtime's current catalog
        # and database were selected through the native catalog API above.
        names = [table.name for table in con._session.catalog.listTables() if not table.isTemporary]
    else:
        names = con.list_tables(database=database)
    return sorted(set(names)), table_kwargs


def add_backend_source(source_id: str, profile: dict) -> DataSource:
    """Connect and discover one namespace, closing every acquired handle on error."""
    profile = validate_profile(profile)
    backend, options = profile["backend"], profile["options"]
    spec = BACKENDS[backend]
    con = None
    stage = "connect"
    try:
        # Keep ownership even if do_connect partially initializes and then fails.
        # Unlike connect(), this also does not retain credentials for reconnect.
        # ibis.<name> is a proxy module; its bound compile method exposes the
        # backend instance without interpolating/importing profile-supplied code.
        con = type(getattr(ibis, backend).compile.__self__)()
        con._can_reconnect = False
        connection_key = profile_id(profile)
        con._con_kwargs = {"d8r_connection_key": connection_key}
        _configure(con, backend, options)
        stage = "discover"
        catalog, schema = _namespace(con, backend, options)
        secrets = profile_secrets(profile)
        source = DataSource(
            id=source_id, display=profile["display"],
            doc=_public(f"{spec.label} · {schema}", secrets),
            kind="ibis", dialect=backend, dir=Path("."), con=con,
            connection_key=connection_key, default_schema=schema,
        )
        names, table_kwargs = _relations(con, backend, catalog, schema)
        for name in names:
            if backend == "sqlite" and name.startswith("sqlite_"):
                continue
            source.datasets[relation_name(schema, name)] = {
                "table": con.table(name, **table_kwargs),
                "doc": _public(f"{name} · {schema}", secrets),
                "rows": None,
                "foreign_keys": (),
            }
        return source
    except BackendConnectionError:
        close_backend_connection(con)
        raise
    except (ImportError, ModuleNotFoundError):
        close_backend_connection(con)
        extra = f"Install from the project directory with: uv sync --extra {spec.extra}."
        runtime = {
            "flink": " Also install a compatible apache-flink/PyFlink and Java runtime; native Windows/Python versions may be unsupported.",
            "pyspark": " A compatible Java runtime is also required.",
            "mssql": " Install a SQL Server ODBC driver separately.",
            "mysql": " mysqlclient may require native MySQL/MariaDB libraries.",
            "risingwave": " psycopg2 may require libpq and native build tools.",
            "datafusion": " A DataFusion wheel must support this platform and Python version.",
        }.get(backend, "")
        raise BackendConnectionError(f"{spec.label} optional driver/runtime is unavailable. {extra}{runtime}") from None
    except Exception:
        close_backend_connection(con)
        if stage == "discover":
            raise BackendConnectionError(f"{spec.label} schema discovery failed; check the selected catalog/schema, metadata permissions and supported column types") from None
        runtime = {
            "flink": " Check the separately installed PyFlink/Java runtime and connector JARs.",
            "pyspark": " Check the installed Java/Spark runtime and master or Spark Connect endpoint.",
            "mssql": " Check that the named SQL Server ODBC driver is installed.",
            "datafusion": " Check the native runtime, settings and CSV/Parquet paths.",
        }.get(backend, "")
        raise BackendConnectionError(f"{spec.label} connection failed; check the endpoint, credentials, TLS and connection options.{runtime}") from None
