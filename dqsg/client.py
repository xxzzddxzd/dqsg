from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit, urlunsplit
import uuid
import time
import requests

from .crypto import (
    STARTUP_KEY, BASE_URL,
    encrypt_request, decrypt_response, xor_bytes, rsa_public_encrypt,
)
from .serialization import BytesReader, BytesWriter
from .parsers import (
    parse_masterdata_response, parse_startup_response, parse_login_response,
    parse_home_info_response, parse_empty_response, parse_user_model_response,
    parse_start_tutorial_response, parse_result_tutorial_response,
    parse_metric_response,
    build_login_request, build_startup_request,
    build_terms_agree_request, build_home_info_request,
    build_adventure_read_request, build_tutorial_read_request,
    build_feature_intro_read_request, build_set_user_name_request,
    build_save_avatar_request,
    build_metric_adventure_skip_request, build_metric_tutorial_request,
    build_metric_low_fps_request, build_metric_device_request,
    build_in_game_start_request, build_in_game_result_request,
    parse_in_game_result_response,
    parse_in_game_stage_skip_response,
    build_gacha_draw_request, parse_gacha_draw_response,
    parse_gacha_fetch_list_response,
    GACHA_METAL_10, GACHA_NORMAL_10, GACHA_TUTORIAL,
    equipment_rarity, equipment_display_name,
    build_deck_save_equipment_request,
    build_present_receive_request,
    build_playable_guide_read_request,
    build_notice_read_all_normal_notices_request,
    build_notice_detail_request,
    build_release_function_unlock_request,
    build_main_area_read_unlock_request,
    build_weapon_growth_level_request,
    build_area_receive_achievement_reward_request,
    build_mission_get_summary_request,
    build_mission_receive_daily_reward_and_progress_reward_request,
    build_mission_receive_achievement_reward_request,
    build_mission_receive_event_reward_request,
    build_mission_receive_daily_reward_request,
    build_mission_receive_daily_progress_reward_request,
    build_mission_receive_weekly_reward_request,
    build_mission_receive_weekly_progress_reward_request,
    build_mission_panel_fetch_request,
    build_mission_panel_receive_reward_request,
    build_user_rank_receive_reward_request,
    build_advertisement_receive_reward_chance_point_card_point_request,
    build_advertisement_receive_reward_ad_chance_orb_request,
    build_expedition_receive_reward_request,
    build_expedition_do_expedition_request,
    build_shop_exchange_exchange_request,
    build_profile_fetch_request,
    build_album_receive_orb_rank_reward_request,
    build_album_receive_enemy_kill_count_reward_request,
)

_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"
_DEFAULT_HEADERS = {
    "accept": "*/*",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "zh-CN,zh-Hans;q=0.9",
    "content-type": "application/json",
    "user-agent": os.environ.get(
        "DQSG_USER_AGENT",
        "DQSG/2649429 CFNetwork/1399 Darwin/22.1.0",
    ),
}
_DEFAULT_MASTERDATA_VERSION = os.environ.get("DQSG_MASTERDATA_VERSION", "3f4f6411725abe85")
_DEFAULT_MASTERDATA_REVISION = int(os.environ.get("DQSG_MASTERDATA_REVISION", "414"))
_DEFAULT_PROXY_COUNTRY = os.environ.get("DQSG_PROXY_COUNTRY", "TW")
_DEFAULT_PROXY_CONFIG_FILE = os.environ.get("DQSG_PROXY_CONFIG_FILE", os.path.join("config", "proxy_pool.json"))
_AUTO_PROXY_ENABLED = os.environ.get("DQSG_PROXY_AUTO", "1").lower() not in {"0", "false", "no", "off"}
_AUTO_PROXY_SOURCES = tuple(
    source.strip()
    for source in os.environ.get(
        "DQSG_PROXY_AUTO_URLS",
        "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/countries/{country}/data.txt,"
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/{country}/data.txt,"
        "https://proxylist.geonode.com/api/proxy-list?limit=50&page=1&sort_by=lastChecked&sort_type=desc&countries={country}&protocols=http%2Chttps%2Csocks4%2Csocks5,"
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country={country}&ssl=all&anonymity=all,"
        "https://www.proxy-list.download/api/v1/get?type=http&country={country}",
    ).split(",")
    if source.strip()
)
_ALLOWED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}


def _color(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}"


def _status_text(status: int) -> str:
    text = f"status={status}"
    return _color(text, _GREEN if status == 1 else _RED)


def _http_status_text(status_code: int) -> str:
    text = f"HTTP {status_code}"
    return _color(text, _GREEN if status_code == 200 else _RED)


def _request_param_text(plaintext: bytes) -> str:
    if not plaintext:
        return "-"
    if len(plaintext) <= 16:
        return plaintext.hex()
    return f"<{len(plaintext)} bytes>"


def _normalize_proxy_url(proxy_url: str) -> str:
    proxy_url = proxy_url.strip()
    if not proxy_url:
        raise ValueError("proxy URL is empty")
    if "://" not in proxy_url:
        proxy_url = "http://" + proxy_url
    parts = _parse_proxy_url(proxy_url)
    if parts is None:
        raise ValueError(f"invalid proxy URL: {proxy_url}")
    return proxy_url


def _parse_proxy_url(proxy_url: str):
    try:
        parts = urlsplit(proxy_url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _ALLOWED_PROXY_SCHEMES:
        return None
    if not parts.hostname or port is None:
        return None
    if not (1 <= port <= 65535):
        return None
    return parts


def _maybe_normalize_proxy_url(proxy_url: str) -> str | None:
    try:
        return _normalize_proxy_url(proxy_url)
    except ValueError:
        return None


def _redact_proxy_url(proxy_url: str) -> str:
    parts = _parse_proxy_url(proxy_url)
    if parts is None:
        return "<invalid proxy>"
    if "@" not in parts.netloc:
        return proxy_url
    userinfo, hostinfo = parts.netloc.rsplit("@", 1)
    username = userinfo.split(":", 1)[0]
    return urlunsplit((parts.scheme, f"{username}:***@{hostinfo}", parts.path, parts.query, parts.fragment))


def _proxy_urls_from_text(text: str) -> list[str]:
    proxies = []
    for match in re.finditer(r"(?:https?|socks5h?|socks4)://[^\s\"'<>]+|\b[\w.-]+:\d+\b", text):
        candidate = match.group(0).rstrip(".,;)]}")
        if "://" not in candidate:
            host = candidate.rsplit(":", 1)[0]
            if "." not in host:
                continue
        proxy_url = _maybe_normalize_proxy_url(candidate)
        if proxy_url:
            proxies.append(proxy_url)
    return proxies


def _dedupe_proxy_urls(proxy_urls: list[str]) -> list[str]:
    result = []
    seen = set()
    for proxy_url in proxy_urls:
        if proxy_url in seen:
            continue
        seen.add(proxy_url)
        result.append(proxy_url)
    return result


def _proxy_url_variants(proxy_url: str) -> list[str]:
    proxy_url = _normalize_proxy_url(proxy_url)
    parts = _parse_proxy_url(proxy_url)
    variants = [proxy_url]
    if parts is not None and parts.scheme == "http" and parts.port == 443:
        variants.append(urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment)))
    return _dedupe_proxy_urls(variants)


def _find_proxy_urls_in_json(value):
    found = []
    if isinstance(value, str):
        return _proxy_urls_from_text(value.strip())
    if isinstance(value, list):
        for item in value:
            found.extend(_find_proxy_urls_in_json(item))
        return found
    if isinstance(value, dict):
        ip = value.get("ip") or value.get("host")
        port = value.get("port")
        if ip and port:
            found.append(f"http://{ip}:{port}")
        for key in ("proxy", "url", "http", "https", "server", "data", "items", "results", "proxies"):
            if key in value:
                found.extend(_find_proxy_urls_in_json(value[key]))
    return found


def _extract_proxy_urls(response_text: str) -> list[str]:
    text = response_text.strip()
    if not text:
        raise ValueError("proxy API returned an empty response")
    if text[0] in "[{":
        found = _find_proxy_urls_in_json(json.loads(text))
        if found:
            return _dedupe_proxy_urls([
                proxy_url
                for proxy in found
                if (proxy_url := _maybe_normalize_proxy_url(proxy))
            ])
    proxies = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        proxies.extend(_proxy_urls_from_text(line))
    if proxies:
        return _dedupe_proxy_urls(proxies)
    proxy_url = _maybe_normalize_proxy_url(text)
    return [proxy_url] if proxy_url else []


def _extract_proxy_url(response_text: str) -> str:
    proxies = _extract_proxy_urls(response_text)
    if not proxies:
        raise ValueError("proxy API did not return any valid proxy URL")
    return proxies[0]


def _proxy_cache_path() -> str | None:
    if os.environ.get("DQSG_PROXY_CACHE", "1").lower() in _FALSE_VALUES:
        return None
    path = os.environ.get("DQSG_PROXY_CACHE_FILE")
    if path is None:
        path = os.path.join("~", ".dqsg", "proxy_cache.json")
    return os.path.expanduser(path)


def _read_proxy_cache() -> dict:
    path = _proxy_cache_path()
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _write_proxy_cache(data: dict):
    path = _proxy_cache_path()
    if not path:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2, sort_keys=True)


def _proxy_config_path() -> str | None:
    path = _DEFAULT_PROXY_CONFIG_FILE
    if not path or path.lower() in _FALSE_VALUES:
        return None
    return os.path.expanduser(path)


def _read_proxy_config() -> dict:
    path = _proxy_config_path()
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _proxy_urls_from_config_value(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_proxy_urls_from_config_value(item))
        return found
    if isinstance(value, dict):
        if value.get("proxy_url"):
            return [value["proxy_url"]]
        found = []
        for key in ("proxy_urls", "proxies", "items"):
            if key in value:
                found.extend(_proxy_urls_from_config_value(value[key]))
        return found
    return []


def _load_configured_proxy_urls(country: str) -> list[str]:
    data = _read_proxy_config()
    if not data:
        return []
    country = country.upper()
    regions = data.get("regions", data)
    if not isinstance(regions, dict):
        return []
    value = regions.get(country) or regions.get(country.lower())
    return _dedupe_proxy_urls([
        proxy_url
        for proxy in _proxy_urls_from_config_value(value)
        if (proxy_url := _maybe_normalize_proxy_url(proxy))
    ])


# ==========================================================================
# Account credentials
# ==========================================================================

ACCT1_STORED_KEY = bytes.fromhex("7a2216ecb295277fc270209284894853145c1874f0534c9214fa414d89a93cad")
ACCT1_USER_ID = 87125091589
ACCT1_CLIENT_UUID = "3f583c1d-a510-4dae-8f92-e301c4e509ef"

ACCT2_STARTUP_RANDOM = bytes.fromhex("f0df1e1e516cda89ae745ad2ca48949d3220e5a96609be962a4828626132605b")
ACCT2_AUTH_KEY = bytes.fromhex("86c531bb0ad099786db2b38cc45c93e78951e29a2eec3a8a453d1299c5259dca")
ACCT2_STORED_KEY = xor_bytes(ACCT2_STARTUP_RANDOM, ACCT2_AUTH_KEY)
ACCT2_USER_ID = 36183493676
ACCT2_CLIENT_UUID = "9fcda925-b5c5-4268-aa9a-60e39ec0e513"

ACCT3_STORED_KEY = bytes.fromhex("cba9ef8a631614a113ccb50e929f4fceff57366f5816910587438741beb6956c")
ACCT3_USER_ID = 62925351098
ACCT3_CLIENT_UUID = "d6a93da1-3193-4e8a-8f96-22d3b8ae7747"

ACCT4_STORED_KEY = bytes.fromhex("bd423ddb522964d8799ceacb9b48bced8158ccdcda6d6e36471f2198971ff0c7")
ACCT4_USER_ID = 96317219522
ACCT4_CLIENT_UUID = "eddfQFzGAhkS0kVKHpJYOwx"  # from firebase device token prefix


class DQSGClient:
    def __init__(self, user_id=None, stored_key=None, client_uuid=None):
        self.session = requests.Session()
        self.session.verify = False
        self.debug = bool(os.environ.get("DQSG_DEBUG"))
        self.session_key = STARTUP_KEY
        self.user_id = int(user_id) if user_id is not None else ACCT2_USER_ID
        if stored_key is None:
            self.stored_key = ACCT2_STORED_KEY
        elif isinstance(stored_key, str):
            self.stored_key = bytes.fromhex(stored_key)
        else:
            self.stored_key = stored_key
        self.client_uuid = client_uuid or ACCT2_CLIENT_UUID
        self.login_key = xor_bytes(self.stored_key, STARTUP_KEY)
        self.request_id = 0
        self.mv = None
        self.last_login_response_raw = None
        self.last_response_raw = None
        self.last_response_endpoint = None
        self.terminal_id = None
        self.startup_random = None
        self.authorization_key = None
        self.proxy_url = None
        self.proxy_country = _DEFAULT_PROXY_COUNTRY
        self.proxy_auto_enabled = _AUTO_PROXY_ENABLED

    @classmethod
    def new_account(cls):
        obj = cls.__new__(cls)
        obj.session = requests.Session()
        obj.session.verify = False
        obj.session_key = STARTUP_KEY
        obj.user_id = None
        obj.stored_key = None
        obj.client_uuid = str(uuid.uuid4())
        obj.login_key = None
        obj.request_id = 0
        obj.mv = None
        obj.last_login_response_raw = None
        obj.last_response_raw = None
        obj.last_response_endpoint = None
        obj.terminal_id = None
        obj.startup_random = None
        obj.authorization_key = None
        obj.proxy_url = None
        obj.proxy_country = _DEFAULT_PROXY_COUNTRY
        obj.proxy_auto_enabled = _AUTO_PROXY_ENABLED
        return obj

    @classmethod
    def from_account_record(cls, record: dict):
        obj = cls(
            user_id=record["user_id"],
            stored_key=record["stored_key"],
            client_uuid=record["client_uuid"],
        )
        obj.terminal_id = record.get("terminal_id")
        startup_random = record.get("startup_random")
        obj.startup_random = bytes.fromhex(startup_random) if startup_random else None
        authorization_key = record.get("authorization_key")
        obj.authorization_key = bytes.fromhex(authorization_key) if authorization_key else None
        login_key = record.get("login_key")
        if login_key:
            obj.login_key = bytes.fromhex(login_key)
        return obj

    def export_account_record(self) -> dict:
        if self.user_id is None or self.stored_key is None or not self.client_uuid:
            raise ValueError("Account credentials are incomplete")
        record = {
            "user_id": int(self.user_id),
            "stored_key": self.stored_key.hex(),
            "client_uuid": self.client_uuid,
        }
        if self.terminal_id:
            record["terminal_id"] = self.terminal_id
        if self.startup_random:
            record["startup_random"] = self.startup_random.hex()
        if self.authorization_key:
            record["authorization_key"] = self.authorization_key.hex()
        if self.login_key:
            record["login_key"] = self.login_key.hex()
        return record

    def debug_log(self, message: str):
        if self.debug:
            print(message)

    def configure_proxy(
        self,
        proxy_url: str = None,
        proxy_api_url: str = None,
        country: str = None,
        proxy_auto: bool = None,
    ):
        proxy_url = proxy_url or os.environ.get("DQSG_PROXY_URL")
        proxy_api_url = proxy_api_url or os.environ.get("DQSG_PROXY_API_URL")
        country = country or _DEFAULT_PROXY_COUNTRY
        self.proxy_country = country
        if proxy_auto is not None:
            self.proxy_auto_enabled = proxy_auto

        if not proxy_url and proxy_api_url:
            url = proxy_api_url.format(country=country, country_lower=country.lower())
            resp = requests.get(url, timeout=float(os.environ.get("DQSG_PROXY_API_TIMEOUT", "10")))
            resp.raise_for_status()
            proxy_url = _extract_proxy_url(resp.text)

        if not proxy_url:
            if proxy_auto is True:
                self._configure_auto_proxy(reason="requested")
            return None

        return self._set_proxy(proxy_url, country=country)

    def _set_proxy(self, proxy_url: str, country: str = None):
        proxy_url = _normalize_proxy_url(proxy_url)
        self.proxy_url = proxy_url
        self.session.proxies.update({
            "http": proxy_url,
            "https": proxy_url,
        })
        country = country or self.proxy_country
        print(f"  [proxy] using {country} proxy: {_redact_proxy_url(proxy_url)}")
        return proxy_url

    def _proxy_probe(self, proxy_url: str) -> bool:
        mode = os.environ.get("DQSG_PROXY_TEST_MODE", "dqsg").lower()
        if mode in {"", "0", "none", "off"}:
            return True
        proxies = {"http": proxy_url, "https": proxy_url}
        timeout = float(os.environ.get("DQSG_PROXY_TEST_TIMEOUT", "8"))
        try:
            if mode == "ipify":
                probe_url = os.environ.get("DQSG_PROXY_TEST_URL", "https://api.ipify.org?format=json")
                resp = requests.get(probe_url, proxies=proxies, timeout=timeout)
                return resp.status_code == 200
            path = "/masterdata/get_version?p=i&id=1&l=zh"
            encrypted = encrypt_request(STARTUP_KEY, path, b"")
            resp = requests.post(
                BASE_URL + path,
                data=encrypted,
                headers=_DEFAULT_HEADERS,
                proxies=proxies,
                timeout=timeout,
                verify=False,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _load_cached_proxy(self, country: str) -> str | None:
        country = country.upper()
        try:
            entry = _read_proxy_cache().get(country)
        except Exception as exc:
            self.debug_log(f"  [proxy] cache read failed: {type(exc).__name__}: {exc}")
            return None
        if isinstance(entry, str):
            proxy_url = entry
        elif isinstance(entry, dict):
            proxy_url = entry.get("proxy_url")
        else:
            return None
        return _maybe_normalize_proxy_url(proxy_url) if proxy_url else None

    def _save_cached_proxy(self, country: str, proxy_url: str):
        country = country.upper()
        try:
            data = _read_proxy_cache()
            data[country] = {
                "proxy_url": _normalize_proxy_url(proxy_url),
                "updated_at": int(time.time()),
            }
            _write_proxy_cache(data)
        except Exception as exc:
            self.debug_log(f"  [proxy] cache write failed: {type(exc).__name__}: {exc}")

    def _configure_auto_proxy(self, reason: str = None) -> bool:
        if self.proxy_url or not self.proxy_auto_enabled:
            return False
        country = self.proxy_country or _DEFAULT_PROXY_COUNTRY
        if reason:
            print(f"  [proxy] {reason}; fetching {country} proxy automatically")
        else:
            print(f"  [proxy] fetching {country} proxy automatically")
        configured_proxies = _load_configured_proxy_urls(country)
        if configured_proxies:
            print(f"  [proxy] testing {len(configured_proxies)} configured {country} proxy candidate(s)")
            for configured_proxy in configured_proxies:
                for probe_url in _proxy_url_variants(configured_proxy):
                    if self._proxy_probe(probe_url):
                        self._set_proxy(probe_url, country=country)
                        self._save_cached_proxy(country, probe_url)
                        return True
                    self.debug_log(f"  [proxy] configured proxy failed: {_redact_proxy_url(probe_url)}")
            print(f"  [proxy] configured {country} proxies failed; refreshing")
        cached_proxy = self._load_cached_proxy(country)
        if cached_proxy:
            for probe_url in _proxy_url_variants(cached_proxy):
                if self._proxy_probe(probe_url):
                    self._set_proxy(probe_url, country=country)
                    if probe_url != cached_proxy:
                        self._save_cached_proxy(country, probe_url)
                    return True
                self.debug_log(f"  [proxy] cached proxy failed: {_redact_proxy_url(probe_url)}")
            print(f"  [proxy] cached {country} proxy failed; refreshing")
        timeout = float(os.environ.get("DQSG_PROXY_API_TIMEOUT", "10"))
        max_candidates = int(os.environ.get("DQSG_PROXY_MAX_CANDIDATES", "20"))
        checked = 0
        for source in _AUTO_PROXY_SOURCES:
            url = source.format(country=country, country_lower=country.lower())
            try:
                resp = requests.get(url, timeout=timeout)
                resp.raise_for_status()
                candidates = _extract_proxy_urls(resp.text)
            except Exception as exc:
                self.debug_log(f"  [proxy] source failed: {url} ({type(exc).__name__}: {exc})")
                continue
            for proxy_url in candidates:
                checked += 1
                if checked > max_candidates:
                    print(f"  [proxy] no usable {country} proxy found after {checked - 1} candidate(s)")
                    return False
                for probe_url in _proxy_url_variants(proxy_url):
                    if self._proxy_probe(probe_url):
                        self._set_proxy(probe_url, country=country)
                        self._save_cached_proxy(country, probe_url)
                        return True
                    self.debug_log(f"  [proxy] probe failed: {_redact_proxy_url(probe_url)}")
        print(f"  [proxy] no usable {country} proxy found")
        return False

    def _key_debug_label(self, key: bytes) -> str:
        if key == STARTUP_KEY:
            return "startupKey"
        if self.login_key is not None and key == self.login_key:
            return "loginKey"
        if key == self.session_key:
            return "sessionKey"
        return "key"

    def _debug_request_details(self, path: str, url: str, key: bytes, plaintext: bytes, encrypted: bytes):
        if not self.debug:
            return
        print(f"  -> path: {path}")
        print(f"  -> request URL: {url}")
        print(f"  -> user-agent: {_DEFAULT_HEADERS['user-agent']}")
        if self.proxy_url:
            print(f"  -> proxy: {_redact_proxy_url(self.proxy_url)}")
        print(f"  -> {self._key_debug_label(key)} ({len(key)} bytes) = {key.hex()}")
        print(f"  -> plaintext ({len(plaintext)} bytes) = {plaintext.hex() if plaintext else '-'}")
        print(f"  -> encrypted ({len(encrypted)} bytes) = {encrypted.hex()}")

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _build_path(self, endpoint: str, with_user=False, with_time=False) -> str:
        self.request_id += 1
        path = f"/{endpoint}?p=i&id={self.request_id}"
        if with_user and self.user_id:
            path += f"&u={self.user_id}"
        if with_time:
            path += f"&t={int(time.time() * 1000)}"
        path += "&l=zh"
        if self.mv:
            path += f"&mv={self.mv}"
        return path

    def _call(self, endpoint: str, plaintext: bytes = b"",
              key: bytes = None, with_user=False, with_time=False) -> bytes:
        if key is None:
            key = self.session_key
        path = self._build_path(endpoint, with_user=with_user, with_time=with_time)
        url = BASE_URL + path
        encrypted = encrypt_request(key, path, plaintext)
        self._debug_request_details(path, url, key, plaintext, encrypted)
        line = f"=== /{endpoint} {_request_param_text(plaintext)}"

        # Save decrypted request body
        safe_endpoint = endpoint.replace("/", "_")
        account_id = str(self.user_id) if self.user_id else "unknown"
        os.makedirs("req", exist_ok=True)
        with open(os.path.join("req", f"req_{safe_endpoint}_{account_id}"), "wb") as f:
            f.write(plaintext)

        last_exc = None
        for attempt in range(1, 4):
            try:
                resp = self.session.post(
                    url,
                    data=encrypted,
                    headers=_DEFAULT_HEADERS,
                )
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.SSLError) as exc:
                last_exc = exc
                if attempt >= 3:
                    print(f"{line} req:{type(exc).__name__}")
                    raise
                line += f" req:{type(exc).__name__}(retry)"
                time.sleep(1.0)
        else:
            raise last_exc
        if resp.status_code != 200:
            retry_tag = "(retry)" if resp.status_code >= 500 else ""
            print(f"{line} req:{_http_status_text(200)} res:{_http_status_text(resp.status_code)}{retry_tag}")
            resp.raise_for_status()
        decrypted = decrypt_response(key, path, resp.content)
        self.last_response_raw = decrypted
        self.last_response_endpoint = endpoint
        print(f"{line} req:{_http_status_text(200)} res:{_http_status_text(resp.status_code)}")

        # Save decrypted response body
        os.makedirs("res", exist_ok=True)
        with open(os.path.join("res", f"res_{safe_endpoint}_{account_id}"), "wb") as f:
            f.write(decrypted)

        return decrypted

    def call_authenticated(self, endpoint: str, plaintext: bytes = b"") -> bytes:
        return self._call(endpoint, plaintext, with_user=True, with_time=True)

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    def masterdata_get_version(self):
        try:
            data = self._call("masterdata/get_version", b"")
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 403:
                raise
            if self._configure_auto_proxy(reason="masterdata hit HTTP 403"):
                try:
                    data = self._call("masterdata/get_version", b"")
                    resp = parse_masterdata_response(data)
                    self.mv = resp["version"]
                    self.debug_log(f"  <- version={resp['version']}, revision={resp['revision']}")
                    return resp
                except requests.HTTPError as retry_exc:
                    if retry_exc.response is None or retry_exc.response.status_code != 403:
                        raise
                    print("  [proxy] masterdata still returned HTTP 403 after proxy retry")
            if not _DEFAULT_MASTERDATA_VERSION:
                raise
            self.mv = _DEFAULT_MASTERDATA_VERSION
            print(
                "  [masterdata] HTTP 403; "
                f"using fallback mv={_DEFAULT_MASTERDATA_VERSION}"
            )
            return {
                "_status": 1,
                "timestamp": 0,
                "revision": _DEFAULT_MASTERDATA_REVISION,
                "version": _DEFAULT_MASTERDATA_VERSION,
                "_fallback": True,
            }
        resp = parse_masterdata_response(data)
        self.mv = resp["version"]
        self.debug_log(f"  <- version={resp['version']}, revision={resp['revision']}")
        return resp

    def login_startup(self):
        startup_random = os.urandom(32)
        mask = rsa_public_encrypt(startup_random)
        terminal_id = str(uuid.uuid4()).upper()
        req = build_startup_request(mask, self.client_uuid, terminal_id)
        self.debug_log(f"  clientUuid={self.client_uuid}")
        self.debug_log(f"  terminalId={terminal_id}")
        self.debug_log(f"  startupRandom={startup_random.hex()[:16]}...")
        data = self._call("login/startup", req, key=STARTUP_KEY)
        resp = parse_startup_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        self.debug_log(f"  <- UserId={resp['UserId']}")
        self.debug_log(f"  <- AuthorizationKey={resp['AuthorizationKey'].hex()[:16]}...")

        self.user_id = resp["UserId"]
        auth_key = resp["AuthorizationKey"]
        self.terminal_id = terminal_id
        self.startup_random = startup_random
        self.authorization_key = auth_key
        self.stored_key = xor_bytes(startup_random, auth_key)
        self.login_key = xor_bytes(self.stored_key, STARTUP_KEY)
        self.debug_log(f"  <- storedKey={self.stored_key.hex()[:16]}...")
        self.debug_log(f"  <- loginKey={self.login_key.hex()[:16]}...")

        return {
            "user_id": self.user_id,
            "stored_key": self.stored_key.hex(),
            "client_uuid": self.client_uuid,
            "startup_random": startup_random.hex(),
            "authorization_key": auth_key.hex(),
            "terminal_id": terminal_id,
        }

    def login_login(self, first_login=False):
        if self.login_key is None:
            raise RuntimeError("login_key is missing; create or load an account first")
        if first_login:
            auth_count = 1
        else:
            random_bytes = os.urandom(32)
            mask = rsa_public_encrypt(random_bytes)
            req = build_login_request(1, mask, self.client_uuid)
            self.debug_log(f"  [probe] sending auth_count=1 to learn server count...")
            data = self._call("login/login", req, key=self.login_key,
                              with_user=True, with_time=False)
            r = BytesReader(data)
            r.read_int()  # status
            server_count = r.read_int()
            r.read_bytes()  # empty session key
            self.debug_log(f"  [probe] server count={server_count}")
            auth_count = server_count + 1

        random_bytes = os.urandom(32)
        mask = rsa_public_encrypt(random_bytes)
        req = build_login_request(auth_count, mask, self.client_uuid)
        self.debug_log(f"  [login] sending auth_count={auth_count}")
        self.debug_log(f"  loginKey={self.login_key.hex()[:16]}...")
        data = self._call("login/login", req, key=self.login_key,
                          with_user=True, with_time=True)
        self.last_login_response_raw = data
        resp = parse_login_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}, AuthCount={resp['AuthorizationCount']}")
        if resp["SessionKey"] and len(resp["SessionKey"]) == 32:
            self.session_key = xor_bytes(random_bytes, resp["SessionKey"])
            self.debug_log(f"  <- sessionKey={self.session_key.hex()[:16]}...")
            self.debug_log(f"  <- ClientId={resp.get('ClientId', '?')}")
            self.debug_log(f"  <- InGameSessionId={resp.get('InGameSessionId')}")
            self.debug_log(f"  <- AssetCdnUrl={resp.get('AssetCdnUrl', '?')}")
        else:
            self.debug_log(f"  <- WARNING: no valid SessionKey (len={len(resp['SessionKey'])})")
        return resp

    def terms_get(self):
        data = self._call("terms/get_terms_eu", b"",
                          key=STARTUP_KEY, with_user=True, with_time=True)
        self.debug_log(f"  <- {len(data)} bytes (HTML terms)")
        return data

    def terms_agree(self):
        req = build_terms_agree_request()
        data = self.call_authenticated("terms/terms_agree_eu", req)
        self.debug_log(f"  <- {len(data)} bytes")
        return data

    def home_fetch_info(self, device_name: str = "iPhone",
                        device_token: str = None,
                        advertising_id: str = None,
                        is_tracking: bool = None,
                        firebase_id: str = None,
                        adjust_id: str = None):
        req = build_home_info_request(
            device_name=device_name,
            device_token=device_token,
            advertising_id=advertising_id,
            is_tracking=is_tracking,
            firebase_id=firebase_id,
            adjust_id=adjust_id,
        )
        data = self.call_authenticated("home/fetch_info", req)
        resp = parse_home_info_response(data)
        if self.debug:
            print(f"  <- {_status_text(resp['_status'])}, PresentCount={resp['PresentCount']}")
            notice = resp["Notice"]
            print(f"  <- {len(notice['MandatoryNotices'])} mandatory, {len(notice['HomeBannerNotices'])} banners")
            for b in notice["HomeBannerNotices"]:
                print(f"     [{b['InformationId']}] {b['TitleText']}  ({b['StartAt']} ~ {b['EndAt']})")
        return resp

    def delete_account(self):
        data = self.call_authenticated("user/delete")
        resp = parse_empty_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    # ------------------------------------------------------------------
    # Tutorial flow
    # ------------------------------------------------------------------

    def in_game_start_tutorial(self):
        data = self.call_authenticated("in_game/start_tutorial")
        resp = parse_start_tutorial_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}, remaining={resp['_remaining']} bytes")
        return resp

    def in_game_result_tutorial(self):
        data = self.call_authenticated("in_game/result_tutorial")
        resp = parse_result_tutorial_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}, remaining={resp['_remaining']} bytes")
        return resp

    def adventure_read(self, adventure_master_id: int):
        req = build_adventure_read_request(adventure_master_id)
        data = self.call_authenticated("adventure/read", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def tutorial_read(self, tutorial_step: int):
        req = build_tutorial_read_request(tutorial_step)
        data = self.call_authenticated("tutorial/read", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def feature_intro_read(self, feature_intro_type: int):
        req = build_feature_intro_read_request(feature_intro_type)
        data = self.call_authenticated("feature_intro/read", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def profile_set_user_name(self, name: str):
        req = build_set_user_name_request(name)
        data = self.call_authenticated("profile/set_user_name", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def avatar_save(self, avatar_id=1, body_id=1, face_id=1,
                    eye_color_id=1, skin_color_id=1,
                    hair_id=1, hair_color_id=1, voice_id=1):
        req = build_save_avatar_request(
            avatar_id, body_id, face_id, eye_color_id,
            skin_color_id, hair_id, hair_color_id, voice_id)
        data = self.call_authenticated("avatar/save", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def metric_tutorial(self):
        req = build_metric_tutorial_request()
        data = self.call_authenticated("metric/tutorial", req)
        resp = parse_metric_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def metric_adventure_skip(self, adventure_master_id: int, command_index: int):
        req = build_metric_adventure_skip_request(adventure_master_id, command_index)
        data = self.call_authenticated("metric/adventure_skip", req)
        resp = parse_metric_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def metric_low_fps_prolonged(self, current_fps: float, duration: float, scene_id: str):
        req = build_metric_low_fps_request(current_fps, duration, scene_id)
        data = self.call_authenticated("metric/low_fps_prolonged", req)
        resp = parse_metric_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def metric_device(self, platform: str = "IPhonePlayer",
                      device_tier: str = "recommended",
                      soc_model: str = "arm64e",
                      device_model: str = "iPhone14,3",
                      system_memory_mb: int = 5626):
        req = build_metric_device_request(platform, device_tier, soc_model,
                                          device_model, system_memory_mb)
        data = self.call_authenticated("metric/device", req)
        resp = parse_metric_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    # ------------------------------------------------------------------
    # Battle (in_game/start, in_game/result)
    # ------------------------------------------------------------------

    def in_game_start(self, stage_master_id: int, deck_index: int = 1,
                      friend_style_id: int = None):
        req = build_in_game_start_request(stage_master_id, deck_index, friend_style_id)
        data = self.call_authenticated("in_game/start", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def in_game_start_raw(self, raw_body: bytes):
        data = self.call_authenticated("in_game/start", raw_body)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def in_game_result(self, stage_master_id: int = None,
                       template_stage_id: int = None,
                       in_game_session_id: int = None,
                       raw_body: bytes = None):
        req = build_in_game_result_request(stage_master_id=stage_master_id,
                                           template_stage_id=template_stage_id,
                                           in_game_session_id=in_game_session_id,
                                           raw_body=raw_body)
        data = self.call_authenticated("in_game/result", req)
        resp = parse_in_game_result_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def in_game_skip_stage(self, stage_master_id: int, count: int = 3):
        """Skip a stage multiple times (requires prior clear)."""
        w = BytesWriter()
        w.write_int(stage_master_id)
        w.write_int(count)
        data = self.call_authenticated("in_game/skip_stage", w.to_bytes())
        resp = parse_in_game_stage_skip_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def matching_room_fetch_multi_data_raw(self, raw_body: bytes):
        data = self.call_authenticated("matching_room/fetch_multi_data", raw_body)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    # ------------------------------------------------------------------
    # Gacha
    # ------------------------------------------------------------------

    def gacha_fetch_top(self):
        data = self.call_authenticated("gacha/fetch_top")
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def gacha_draw(self, gacha_master_id: int):
        """Draw from a gacha pool. Returns structured reward data."""
        req = build_gacha_draw_request(gacha_master_id)
        data = self.call_authenticated("gacha/draw", req)
        resp = parse_gacha_draw_response(data)
        if self.debug:
            print(f"  <- {_status_text(resp['_status'])}, {resp['reward_count']} rewards")
            for rw in resp["rewards"]:
                star = '★' * rw['rarity']
                print(f"     {star} {rw['display']}  (mid={rw['content_master_id']})")
        return resp

    def gacha_fetch_list(self):
        data = self.call_authenticated("gacha/fetch_list")
        resp = parse_gacha_fetch_list_response(data)
        self.debug_log(
            f"  <- {_status_text(resp['_status'])}, draw_count={resp['draw_count']}, "
            f"pools={len(resp['gacha_ids'])}"
        )
        return resp

    # ------------------------------------------------------------------
    # Deck
    # ------------------------------------------------------------------

    def deck_save_style_equipment(self, raw_body: bytes):
        req = build_deck_save_equipment_request(raw_body)
        data = self.call_authenticated("deck/save_style_equipment", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def deck_save_auto_style_equipment(self, raw_body: bytes):
        req = build_deck_save_equipment_request(raw_body)
        data = self.call_authenticated("deck/save_auto_style_equipment", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    # ------------------------------------------------------------------
    # Present
    # ------------------------------------------------------------------

    def present_fetch(self):
        data = self.call_authenticated("present/fetch")
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def present_receive(self, present_ids: list[int]):
        req = build_present_receive_request(present_ids)
        data = self.call_authenticated("present/receive", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def playable_guide_read(self, guide_id: int):
        req = build_playable_guide_read_request(guide_id)
        data = self.call_authenticated("playable_guide/read", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def notice_fetch_notices(self):
        data = self.call_authenticated("notice/fetch_notices")
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def notice_read_all_normal_notices(self, notice_ids: list[int]):
        req = build_notice_read_all_normal_notices_request(notice_ids)
        data = self.call_authenticated("notice/read_all_normal_notices", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def notice_fetch_detail(self, notice_id: int):
        req = build_notice_detail_request(notice_id)
        data = self.call_authenticated("notice/fetch_notice_detail", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def billing_update_web_store(self):
        data = self.call_authenticated("billing/update_web_store")
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def release_function_unlock(self, function_id: int):
        req = build_release_function_unlock_request(function_id)
        data = self.call_authenticated("release_function/unlock", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def main_area_read_unlock(self, area_master_id: int, area_difficulty: int):
        req = build_main_area_read_unlock_request(area_master_id, area_difficulty)
        data = self.call_authenticated("main_area/read_unlock", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def area_receive_achievement_reward(self, area_achievement_ids: list[int]):
        req = build_area_receive_achievement_reward_request(area_achievement_ids)
        data = self.call_authenticated("area/receive_achievement_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_get_summary(self):
        req = build_mission_get_summary_request()
        data = self.call_authenticated("mission/get_mission_summary", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_daily_reward_and_progress_reward(
        self,
        mission_ids: list[int],
        progress_reward_id: int,
        raw_body: bytes | None = None,
    ):
        if raw_body is None:
            req = build_mission_receive_daily_reward_and_progress_reward_request(
                mission_ids,
                progress_reward_id,
            )
        else:
            req = raw_body
        data = self.call_authenticated(
            "mission/receive_mission_daily_reward_and_daily_progress_reward",
            req,
        )
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_achievement_reward(self, mission_ids: list[int]):
        req = build_mission_receive_achievement_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_achievement_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_event_reward(self, mission_ids: list[int]):
        req = build_mission_receive_event_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_event_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_daily_reward(self, mission_ids: list[int]):
        req = build_mission_receive_daily_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_daily_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_daily_progress_reward(self, mission_ids: list[int]):
        req = build_mission_receive_daily_progress_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_daily_progress_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_weekly_reward(self, mission_ids: list[int]):
        req = build_mission_receive_weekly_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_weekly_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_receive_weekly_progress_reward(self, mission_ids: list[int]):
        req = build_mission_receive_weekly_progress_reward_request(mission_ids)
        data = self.call_authenticated("mission/receive_mission_weekly_progress_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_panel_fetch(self, mission_panel_master_id: int):
        req = build_mission_panel_fetch_request(mission_panel_master_id)
        data = self.call_authenticated("mission_panel/fetch_mission", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def mission_panel_receive_reward(self, mission_panel_master_id: int):
        req = build_mission_panel_receive_reward_request(mission_panel_master_id)
        data = self.call_authenticated("mission_panel/receive_mission_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def user_rank_receive_reward(self):
        req = build_user_rank_receive_reward_request()
        data = self.call_authenticated("user_rank/receive_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def advertisement_receive_reward_chance_point_card_point(self):
        req = build_advertisement_receive_reward_chance_point_card_point_request()
        data = self.call_authenticated("advertisement/receive_reward_chance_point_card_point", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def advertisement_receive_reward_ad_chance_orb(self, orb_master_id: int = 100007):
        req = build_advertisement_receive_reward_ad_chance_orb_request(orb_master_id)
        data = self.call_authenticated("advertisement/receive_reward_ad_chance_orb", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def expedition_receive_reward(self, expedition_id: int = 1):
        req = build_expedition_receive_reward_request(expedition_id)
        data = self.call_authenticated("expedition/receive_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def expedition_do_expedition(
        self,
        expedition_id: int = 1,
        expedition_master_id: int = 105,
        user_style_id: int = 0,
    ):
        req = build_expedition_do_expedition_request(
            expedition_id,
            expedition_master_id,
            user_style_id,
        )
        data = self.call_authenticated("expedition/do_expedition", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def shop_exchange_exchange(self, exchange_master_id: int, count: int = 1):
        req = build_shop_exchange_exchange_request(exchange_master_id, count)
        data = self.call_authenticated("shop_exchange/exchange", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def profile_fetch(self):
        req = build_profile_fetch_request(self.user_id)
        data = self.call_authenticated("profile/fetch", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def weapon_growth_level(self, user_weapon_id: int, consume_content_list: list[tuple[int, int, int]]):
        req = build_weapon_growth_level_request(user_weapon_id, consume_content_list)
        data = self.call_authenticated("weapon/growth_level", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def album_receive_orb_rank_reward(self, reward_ids: list[int]):
        req = build_album_receive_orb_rank_reward_request(reward_ids)
        data = self.call_authenticated("album/receive_orb_rank_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp

    def album_receive_enemy_kill_count_reward(self, reward_ids: list[int]):
        req = build_album_receive_enemy_kill_count_reward_request(reward_ids)
        data = self.call_authenticated("album/receive_enemy_kill_count_reward", req)
        resp = parse_user_model_response(data)
        self.debug_log(f"  <- {_status_text(resp['_status'])}")
        return resp
