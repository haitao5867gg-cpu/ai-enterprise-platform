"""Where connectors may be registered (docs/api/datasources.md, 地址检查).

``check_url`` runs when someone tests or saves a connector address: its format,
credentials in it, cloud metadata addresses, the platform's own services, then the
allowlist (AEP_CONNECTOR_HOSTS), stopping at the first that fails. It only reads
the address: host names are not resolved. Sources already registered are not
checked again. ``mask_url`` and ``same_url`` take any stored text and never raise.
"""

import ipaddress
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote_plus, urlsplit

import httpx2
from psycopg.conninfo import conninfo_to_dict

if TYPE_CHECKING:
    from aep.config import Settings

IP = ipaddress.IPv4Address | ipaddress.IPv6Address
Code = Literal["bad_url", "credentials", "metadata", "internal_host", "host_not_allowed"]

MAX_LENGTH = 2048
DEFAULT_PORTS = {"http": 80, "https": 443}
# Ports of the platform's own services when their address leaves it out.
SERVICE_PORTS = {**DEFAULT_PORTS, "redis": 6379, "rediss": 6379, "postgres": 5432}
SENSITIVE = re.compile(r"pass|pwd|token|secret|key|auth|credential|signature", re.I)
SENSITIVE_KEYS = ("sig", "code")
# Database addresses, also with a driver (postgresql+psycopg2://, mongodb+srv://).
DATABASE = re.compile(
    r"^(postgres|postgresql|mysql|mariadb|sqlserver|mssql|oracle|mongodb)(\+[a-z0-9_]+)?://|^jdbc:",
    re.I,
)
PRINTABLE = re.compile(r"^[\x21-\x7e]+$")
BROADCAST = ipaddress.IPv4Address("255.255.255.255")
THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
HOSTNAME = re.compile(r"^[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?$")
METADATA_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "169.254.0.0/16",
        "fe80::/10",
        "fd00:ec2::254/128",
        "168.63.129.16/32",  # Azure
        "100.100.100.200/32",  # Alibaba Cloud
    )
]
# Services of the local stack (docker-compose.yml) that are never connectors; with
# any host starting with INTERNAL_PREFIX (Langfuse's).
INTERNAL_HOSTS = (
    "api",
    "web",
    "sandbox-relay",
    "connector-sandbox",
    "litellm",
    "postgres",
    "redis",
    "source-postgres",
    "source-mysql",
)
INTERNAL_PREFIX = "langfuse-"
# The other names Kubernetes gives a Service: <name>.<namespace>, optionally followed by
# .svc and the cluster domain (.svc.cluster.local). An internal service is denied by
# every one of them, so a suffix rule over the platform's namespace does not let it in.
CLUSTER_ALIAS = re.compile(r"([^.]+)\.[^.]+(?:\.svc(?:\.[^.]+)*)?")

TEXT = {
    "bad_url": (
        "地址格式不对。请填完整的连接器地址，例如 http://connector-postgres:8001/mcp："
        "以 http:// 或 https:// 开头，不含空格、中文和 # 号，端口在 1～65535 之间。"
    ),
    "database_url": (
        "这是数据库的地址。这里要填连接器的地址（一般以 /mcp 结尾），"
        "数据库地址和账号密码配在连接器上。"
    ),
    "credentials": (
        "地址里不能带账号密码，也不能带 password、token、secret、key 这类参数。"
        "数据库账号密码配在连接器上，平台只保存地址。"
    ),
    "metadata": "不能使用云服务器的元数据地址（如 169.254.169.254）。",
    "internal_host": (
        "这是平台内部服务的地址（平台本身、代码沙箱、模型网关、Langfuse、数据库或 Redis），"
        "不能登记。"
    ),
    "host_not_allowed": (
        "主机 {host} 不在允许的范围内，请联系管理员把它加进 AEP_CONNECTOR_HOSTS。"
    ),
}


class Rejected(Exception):
    """The address may not be registered; ``code`` is the problem, ``message`` says why."""

    def __init__(self, code: Code, text: str | None = None, **fmt: str) -> None:
        self.code = code
        self.message = TEXT[text or code].format(**fmt)
        super().__init__(self.message)


@dataclass(frozen=True)
class HostRule:
    """One entry of AEP_CONNECTOR_HOSTS. ``port`` None is the scheme's default port,
    unless ``any_port``; with ``suffix``, ``host`` is the domain subdomains end in."""

    host: str
    port: int | None = None
    any_port: bool = False
    suffix: bool = False


@dataclass(frozen=True)
class Address:
    scheme: str
    host: str  # lowercase; an IP as its canonical text (an IPv4-mapped IPv6 one as IPv4)
    port: int  # as written, else the scheme's default
    ip: IP | None
    url: str  # normalised: what gets saved


def _ip(host: str) -> IP | None:
    """The IP a host names literally (an IPv4-mapped IPv6 address as IPv4), else None."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _host_key(host: str) -> str:
    ip = _ip(host)
    # A configured FQDN may end with a dot (sandbox-relay.aep.svc.cluster.local.).
    return str(ip) if ip is not None else host.lower().removesuffix(".")


def _is_metadata(ip: IP) -> bool:
    return any(ip in net for net in METADATA_NETS)


def _is_loopback(host: str) -> bool:
    ip = _ip(host)
    if ip is None:
        return host == "localhost" or host.endswith(".localhost")
    return ip.is_loopback


def _display(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_rule(text: str) -> HostRule:
    """One AEP_CONNECTOR_HOSTS entry; ValueError when it is not one of the documented forms."""
    raw = text.strip().lower()
    if raw.startswith("["):
        m = re.fullmatch(r"\[([^\]]+)\](?::(.*))?", raw)
        if m is None or not isinstance(ipaddress.ip_address(m.group(1)), ipaddress.IPv6Address):
            raise ValueError(text)
        host, port_text, suffix = _host_key(m.group(1)), m.group(2) or "", False
    else:
        host, _, port_text = raw.partition(":")
        suffix = host.startswith("*.")
        host = host.removeprefix("*.")
        if not HOSTNAME.fullmatch(host) or ".." in host or (suffix and _ip(host) is not None):
            raise ValueError(text)
        host = _host_key(host)
    if port_text == "*":
        return HostRule(host, any_port=True, suffix=suffix)
    if not port_text:
        return HostRule(host, suffix=suffix)
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError(text)
    return HostRule(host, int(port_text), suffix=suffix)


def _service(url: str) -> tuple[str, int] | None:
    """A configured URL's host and port, None when it does not parse."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port or SERVICE_PORTS.get(parts.scheme.lower())
    except ValueError:
        return None
    if not parts.hostname or port is None:
        return None
    return _host_key(parts.hostname), port


def allowed_rules(s: "Settings") -> list[HostRule]:
    """AEP_CONNECTOR_HOSTS, and the exact host and port of the files connector and of
    every default data source."""
    rules = [parse_rule(r) for r in s.connector_hosts]
    urls = [s.files_connector_url, *(d.get("connector_url", "") for d in s.default_datasources)]
    for url in urls:
        if (found := _service(url)) is not None:
            rules.append(HostRule(*found))
    return rules


def _database_hosts(database_url: str) -> list[tuple[str, int]]:
    try:
        info = conninfo_to_dict(database_url)
    except Exception:  # noqa: BLE001 - a URL psycopg cannot read names no host
        return []
    found = []
    for key in ("host", "hostaddr"):
        hosts = str(info.get(key) or "").split(",")
        ports = str(info.get("port") or "").split(",")
        for i, host in enumerate(hosts):
            if not host or host.startswith("/"):
                continue  # a Unix socket
            port = ports[i] if i < len(ports) else ports[0]
            found.append((_host_key(host), int(port) if port.isdigit() else 5432))
    return found


def internal_targets(s: "Settings") -> list[tuple[str, int | None]]:
    """The platform's own services, as (host, port); port None means any port. A
    loopback host only has its own port, as several services share it."""
    found: list[tuple[str, int]] = []
    for url in (s.web_url, s.sandbox_url, s.llm_gateway_url, s.langfuse_host, s.redis_url):
        if (service := _service(url)) is not None:
            found.append(service)
    found += _database_hosts(s.database_url)
    targets: list[tuple[str, int | None]] = [(h, None) for h in INTERNAL_HOSTS]
    for host, port in found:
        targets.append((host, port) if _is_loopback(host) else (host, None))
    return targets


def _parse(text: str) -> Address:
    """Rule 1, the format."""
    if DATABASE.match(text):
        raise Rejected("bad_url", "database_url")
    if len(text) > MAX_LENGTH or not PRINTABLE.match(text) or "\\" in text or "#" in text:
        raise Rejected("bad_url")
    try:
        parts = urlsplit(text)
        written = parts.port
    except ValueError as exc:
        raise Rejected("bad_url") from exc
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if scheme not in DEFAULT_PORTS or not host or written == 0:
        raise Rejected("bad_url")
    if _ip(host) is None and re.fullmatch(r"[0-9a-fx.]+", host):
        _numeric(host)
    try:
        other = httpx2.URL(text)
    except Exception as exc:  # noqa: BLE001 - whatever does not parse is the wrong format
        raise Rejected("bad_url") from exc
    port = written or DEFAULT_PORTS[scheme]
    # Two parsers must agree on where the request goes.
    if _host_key(other.host) != _host_key(host) or (other.port or DEFAULT_PORTS[scheme]) != port:
        raise Rejected("bad_url")
    ip = _ip(host)
    if ip is None:
        if not HOSTNAME.fullmatch(host) or ".." in host:
            raise Rejected("bad_url")
    elif ip.is_unspecified or ip.is_multicast or ip == BROADCAST or ip in THIS_NETWORK:
        raise Rejected("bad_url")
    shown = f"[{ipaddress.ip_address(host).compressed}]" if ":" in host else host
    netloc = shown if port == DEFAULT_PORTS[scheme] else f"{shown}:{port}"
    url = f"{scheme}://{netloc}{parts.path}" + (f"?{parts.query}" if parts.query else "")
    return Address(scheme, _host_key(host), port, ip, url)


def _numeric(host: str) -> None:
    """A host that is an IPv4 address in another notation (2852039166, 0xa9fea9fe):
    metadata if it is one, otherwise the wrong format."""
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return
    raise Rejected("metadata" if _is_metadata(ipaddress.IPv4Address(packed)) else "bad_url")


def _credentials(text: str) -> None:
    """Rule 2: user info, or a query parameter whose name looks like a secret."""
    parts = urlsplit(text)
    if "@" in parts.netloc:
        raise Rejected("credentials")
    for item in re.split(r"[&;]", parts.query):
        key = unquote_plus(item.partition("=")[0]).strip().lower()
        if key in SENSITIVE_KEYS or SENSITIVE.search(key):
            raise Rejected("credentials")


def _service_name(host: str) -> str:
    """The Service a host names in a cluster: ``name`` for ``name.namespace``,
    ``name.namespace.svc`` and ``name.namespace.svc.cluster.local``; else the host."""
    found = CLUSTER_ALIAS.fullmatch(host) if _ip(host) is None else None
    return found.group(1) if found else host


def _internal(address: Address, targets: Iterable[tuple[str, int | None]]) -> bool:
    if address.host.startswith(INTERNAL_PREFIX):
        return True
    name = _service_name(address.host)
    loopback = _is_loopback(address.host)
    for host, port in targets:
        if port is None and name == _service_name(host):
            return True
        if port is not None and loopback and _is_loopback(host) and address.port == port:
            return True
    return False


def _allowed(address: Address, rules: Iterable[HostRule]) -> bool:
    loopback = _is_loopback(address.host)
    for rule in rules:
        if rule.suffix:
            if not address.host.endswith("." + rule.host):
                continue
        elif address.host != rule.host:
            continue
        if loopback and (rule.suffix or rule.any_port or rule.port is None):
            continue  # loopback only with its host and port written out
        if rule.any_port or address.port == (rule.port or DEFAULT_PORTS[address.scheme]):
            return True
    return False


def check_url(url: str, s: "Settings") -> str:
    """The address, normalised, if a connector may be registered there; raises
    ``Rejected`` with the first rule it breaks."""
    text = url.strip()
    address = _parse(text)
    _credentials(text)
    if address.ip is not None and _is_metadata(address.ip):
        raise Rejected("metadata")
    if _internal(address, internal_targets(s)):
        raise Rejected("internal_host")
    if not _allowed(address, allowed_rules(s)):
        raise Rejected("host_not_allowed", host=_display(address.host, address.port))
    return address.url


def mask_url(url: str) -> str:
    """The address with any user info and the values of secret-looking parameters
    replaced by ***, for showing and logging. Takes any text and never raises."""
    try:
        return _mask(url)
    except Exception:  # noqa: BLE001 - never show what could not be read
        return "***"


def _mask(url: str) -> str:
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "", url
    if "@" in rest:
        # A stored password may hold an unencoded / ? or #, so everything up to the last
        # @ counts as user info. An @ in a path or query over-masks; this is only display.
        rest = "***@" + rest.rpartition("@")[2]
    end = min([i for i in (rest.find(c) for c in "/?#") if i != -1], default=len(rest))
    authority, tail = rest[:end], rest[end:]

    def value(m: re.Match[str]) -> str:
        key = unquote_plus(m.group(2)).strip().lower()
        secret = key in SENSITIVE_KEYS or SENSITIVE.search(key)
        return f"{m.group(1)}{m.group(2)}=***" if secret else m.group(0)

    # Parameters also hide in the host part (h;password=x) of addresses that never parsed.
    authority = re.sub(r"(;)([^=;]*)=([^;]*)", value, authority)
    tail = re.sub(r"([?&;#])([^=&;#]*)=([^&;#]*)", value, tail)
    return f"{scheme}{sep}{authority}{tail}"


def same_url(a: str, b: str) -> bool:
    """Whether two addresses name the same endpoint: scheme and host in any case, the
    default port written or not, a trailing / or not. False when either does not parse."""
    if a == b:
        return True
    try:
        return _canonical(a) == _canonical(b)
    except Exception:  # noqa: BLE001 - stored text may be anything
        return False


def _canonical(url: str) -> tuple[object, ...]:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = _host_key(parts.hostname or "")
    port = parts.port or DEFAULT_PORTS.get(scheme)
    path = parts.path.rstrip("/")
    return (scheme, parts.username, parts.password, host, port, path, parts.query, parts.fragment)
