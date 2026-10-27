"""サーバー上の設定ファイル（TOML）を読む。設定ファイルはGitに入れない。"""

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class Site:
    name: str
    claude_api_key: str
    client_key_hashes: frozenset[str]


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float


@dataclass(frozen=True)
class Settings:
    sites: dict[str, Site]
    data_dir: Path
    processes_dir: Path
    log_file: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8001
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_tokens: int = 32000
    claude_timeout_seconds: float = 300
    claude_max_retries: int = 2
    job_timeout_seconds: float = 1200
    """Claude の呼び出し全体（SDK の再試行を含む）の上限。"""
    max_attempts: int = 3
    max_upload_bytes: int = 20 * 1024 * 1024
    upload_slots: int = 2
    max_pages: int = 20
    render_dpi: int = 200
    image_long_edge: int = 2576
    max_request_image_bytes: int = 28 * 1024 * 1024
    """Claude へ送る画像の合計（base64）の上限。API の要求の上限は 32MB。"""
    render_timeout_seconds: float = 120
    prices: dict[str, Price] = field(default_factory=dict)


def hash_client_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def load_settings(path: Path) -> Settings:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SettingsError(f"設定ファイルを読めない: {path}: {error}") from error
    return parse_settings(raw, base_dir=path.parent)


def parse_settings(raw: dict, base_dir: Path) -> Settings:
    try:
        return _parse_settings(raw, base_dir)
    except SettingsError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        # 値は書かない（キーが入っていることがある）
        raise SettingsError(f"設定の項目が正しくない: {type(error).__name__}") from error


def _parse_settings(raw: dict, base_dir: Path) -> Settings:
    raw = dict(raw)
    sites_raw = raw.pop("sites", None)
    if not sites_raw:
        raise SettingsError("拠点（[sites.<名前>]）が1つもない")
    sites: dict[str, Site] = {}
    seen: set[str] = set()
    for name, site_raw in sites_raw.items():
        hashes = frozenset(h.lower() for h in site_raw.get("client_key_sha256", []))
        if not hashes:
            raise SettingsError(f"拠点 {name} に client_key_sha256 がない")
        if overlap := hashes & seen:
            raise SettingsError(f"同じ呼び出し元キーが複数の拠点にある: {sorted(overlap)}")
        seen |= hashes
        if not site_raw.get("claude_api_key"):
            raise SettingsError(f"拠点 {name} に claude_api_key がない")
        sites[name] = Site(name, site_raw["claude_api_key"], hashes)

    prices = {
        model: Price(float(p["input_per_mtok"]), float(p["output_per_mtok"]))
        for model, p in raw.pop("prices", {}).items()
    }

    def resolve(value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (base_dir / p).resolve()

    for key in ("data_dir", "processes_dir"):
        if key not in raw:
            raise SettingsError(f"{key} がない")
        raw[key] = resolve(raw[key])
    if "log_file" in raw:
        raw["log_file"] = resolve(raw["log_file"])
    try:
        settings = Settings(sites=sites, prices=prices, **raw)
    except TypeError as error:
        raise SettingsError(f"設定の項目が正しくない: {error}") from error
    if settings.max_pages > 20 and settings.image_long_edge > 2000:
        # Claude API は、画像が20枚を超える要求では1枚の長辺を 2000px までに制限する
        raise SettingsError("max_pages が20を超えるときは image_long_edge を2000以下にする")
    return settings
