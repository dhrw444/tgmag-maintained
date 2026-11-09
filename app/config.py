"""环境配置。所有可调参数从 .env 读入，进程内只读。

为什么用 pydantic-settings 而不是 os.environ 直接取：每个字段都带上下界，
非法值在启动时就炸，而不是等某个分支跑到才炸。部署时一个 typo 会让进程
起不来并打印清楚的原因，这比运行时静默用一个错的值好。

几个字段的取值不是最终形态，而是"原始字符串"（`*_raw`），由 validator
规整后再通过 property 暴露解析结果 —— 这样校验只做一次，读取不重复解析。
"""

from __future__ import annotations

import json
import re
from functools import cached_property
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# 后端名允许的别名。写成独立映射而不是 if 链，是因为同一个后端在历史配置里
# 有多个写法（cf / cf-tempmail / temp-mail 都指 cloudflare），删掉任何一个
# 都会让旧 .env 启动失败。
EMAIL_BACKEND_ALIASES = {
    "cloudflare": "cloudflare",
    "cf": "cloudflare",
    "cf-tempmail": "cloudflare",
    "temp-mail": "cloudflare",
    "tempmail": "cloudflare",
    "gmail": "gmail",
}

# 域名格式与 login_email_protection.DOMAIN_PATTERN 同一条规则。
# 这里重复一份是因为 config 不能反过来 import services（会形成循环依赖），
# 改动域名规则时两处都要改。
_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def normalize_email_backend(value: str) -> str:
    """把后端别名归一成 cloudflare / gmail，未知值抛 ValueError。"""
    normalized = EMAIL_BACKEND_ALIASES.get(value.strip().lower())
    if normalized is None:
        raise ValueError("email backend must be cloudflare or gmail")
    return normalized


def parse_domain_backends(value: str) -> dict[str, str]:
    """解析 `domain=backend,domain=backend` 形式的域名→后端路由表。

    空串返回空字典。重复域名直接报错而不是后者覆盖前者 —— 静默覆盖会让
    部署者以为自己配了两条规则，实际只有一条生效。
    """
    routes: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("LOGIN_EMAIL_DOMAIN_BACKENDS entries must use domain=backend")
        domain, backend = item.split("=", 1)
        normalized_domain = domain.strip().lower().lstrip("@")
        if not normalized_domain:
            raise ValueError("LOGIN_EMAIL_DOMAIN_BACKENDS contains an empty domain")
        if normalized_domain in routes:
            raise ValueError(
                f"LOGIN_EMAIL_DOMAIN_BACKENDS contains duplicate domain: {normalized_domain}"
            )
        routes[normalized_domain] = normalize_email_backend(backend)
    return routes


class Settings(BaseSettings):
    """进程级配置。模块末尾的 `settings` 是唯一实例。

    `extra="ignore"` 是刻意的：.env 里可能有新版本已删除的旧键，
    忽略它们让升级不用手工清 .env。
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(..., alias="BOT_TOKEN")
    tg_api_id: int = Field(..., alias="TG_API_ID")
    tg_api_hash: str = Field(..., alias="TG_API_HASH")
    # NoDecode 关掉 pydantic-settings 对 list 字段的 JSON 解码，
    # 交给下面的 parse_admin_ids 处理逗号分隔、JSON、单整数三种写法。
    admin_ids: Annotated[list[int], NoDecode] = Field(..., alias="ADMIN_IDS")
    database_url: str = Field(..., alias="DATABASE_URL")
    # 用于加密库里所有敏感字段（手机号、session、2FA 密码、邮箱）。
    # 换掉它等于让已有数据全部无法解密，不要在运行中改。
    fernet_key: str = Field(..., alias="FERNET_KEY")
    session_dir: Path = Field(default=Path("./data/sessions"), alias="SESSION_DIR")
    backup_dir: Path = Field(default=Path("./data/backups"), alias="BACKUP_DIR")
    default_rate_max_actions: int = Field(default=8, ge=1, le=1000, alias="DEFAULT_RATE_MAX_ACTIONS")
    default_rate_per_seconds: int = Field(default=60, ge=1, le=86400, alias="DEFAULT_RATE_PER_SECONDS")
    default_jitter_min: int = Field(default=2, ge=0, le=3600, alias="DEFAULT_JITTER_MIN")
    default_jitter_max: int = Field(default=6, ge=0, le=3600, alias="DEFAULT_JITTER_MAX")
    # 后台服务状态检查的间隔。每个周期会对每个活跃账号发一次 SpamBot 查询，
    # 间隔越短对 Telegram 越不友好，也不要指望调小它能更快发现限流。
    service_monitor_interval_seconds: int = Field(
        default=300, ge=30, alias="SERVICE_MONITOR_INTERVAL_SECONDS"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    mini_app_enabled: bool = Field(default=False, alias="MINI_APP_ENABLED")
    # 默认只听 127.0.0.1。要让容器/反代访问必须显式改成 0.0.0.0，
    # 这是刻意的默认值，不要为了"方便"改掉它。
    mini_app_host: str = Field(default="127.0.0.1", alias="MINI_APP_HOST")
    mini_app_port: int = Field(default=8080, ge=1, le=65535, alias="MINI_APP_PORT")
    mini_app_public_url: str = Field(default="", alias="MINI_APP_PUBLIC_URL")
    mini_app_auth_max_age_seconds: int = Field(
        default=3600, ge=60, le=86400, alias="MINI_APP_AUTH_MAX_AGE_SECONDS"
    )
    login_email_protection_enabled: bool = Field(
        default=True, alias="LOGIN_EMAIL_PROTECTION_ENABLED"
    )
    login_email_alias_domains_raw: str = Field(default="", alias="LOGIN_EMAIL_ALIAS_DOMAINS")
    login_email_domain_backends_raw: str = Field(default="", alias="LOGIN_EMAIL_DOMAIN_BACKENDS")
    temp_mail_webhook_secret: str = Field(default="", alias="TEMP_MAIL_WEBHOOK_SECRET")
    login_email_gmail_username: str = Field(default="", alias="LOGIN_EMAIL_GMAIL_USERNAME")
    login_email_gmail_app_password: str = Field(default="", alias="LOGIN_EMAIL_GMAIL_APP_PASSWORD")
    login_email_imap_host: str = Field(default="imap.gmail.com", alias="LOGIN_EMAIL_IMAP_HOST")
    login_email_imap_port: int = Field(default=993, ge=1, le=65535, alias="LOGIN_EMAIL_IMAP_PORT")
    login_email_imap_folder: str = Field(default="INBOX", alias="LOGIN_EMAIL_IMAP_FOLDER")
    login_email_sender: str = Field(default="noreply@telegram.org", alias="LOGIN_EMAIL_SENDER")
    login_email_poll_timeout_seconds: int = Field(
        default=300, ge=30, le=7200, alias="LOGIN_EMAIL_POLL_TIMEOUT_SECONDS"
    )
    # Gmail IMAP 轮询间隔。设成 1 秒不会更快拿到验证码 —— Telegram 发信本身
    # 就要几秒，而 IMAP 频繁登录会触发 Gmail 的连接频率限制。
    login_email_poll_interval_seconds: int = Field(
        default=3, ge=1, le=30, alias="LOGIN_EMAIL_POLL_INTERVAL_SECONDS"
    )
    login_email_catchup_seconds: int = Field(
        default=180, ge=0, le=3600, alias="LOGIN_EMAIL_CATCHUP_SECONDS"
    )

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> list[int]:
        """接受三种写法：JSON 列表、逗号分隔、单个整数。

        空列表直接报错：没有管理员的 Bot 无法操作，静默启动只会让人以为部署成功了。
        """
        if isinstance(value, list):
            return [int(v) for v in value]
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                decoded = json.loads(stripped)
                if not isinstance(decoded, list):
                    raise ValueError("ADMIN_IDS JSON value must be a list")
                parsed = [int(v) for v in decoded]
            else:
                parsed = [int(v.strip()) for v in stripped.split(",") if v.strip()]
            if parsed:
                return parsed
            raise ValueError("ADMIN_IDS must not be empty")
        raise ValueError("ADMIN_IDS must be a comma separated list, JSON list, or integer")

    @field_validator("login_email_alias_domains_raw")
    @classmethod
    def validate_login_email_alias_domains(cls, value: str) -> str:
        """校验并归一化别名域名列表，同时去重。"""
        domains = [item.strip().lower().lstrip("@") for item in value.split(",") if item.strip()]
        invalid = [domain for domain in domains if not _DOMAIN_PATTERN.fullmatch(domain)]
        if invalid:
            raise ValueError(f"invalid login email alias domain: {invalid[0]}")
        if len(domains) != len(set(domains)):
            raise ValueError("LOGIN_EMAIL_ALIAS_DOMAINS contains duplicates")
        return ",".join(domains)

    @field_validator("login_email_domain_backends_raw")
    @classmethod
    def validate_login_email_domain_backends(cls, value: str) -> str:
        """按 parse_domain_backends 解析一遍，把归一化后的结果写回原始字符串。"""
        routes = parse_domain_backends(value)
        return ",".join(f"{domain}={backend}" for domain, backend in routes.items())

    @field_validator("login_email_gmail_username")
    @classmethod
    def normalize_gmail_username(cls, value: str) -> str:
        """Gmail 用户名统一成小写去空白：IMAP 登录区分大小写，
        而用户从邮箱设置页复制出来的往往带空格或首字母大写。
        """
        return value.strip().lower()

    @field_validator("login_email_gmail_app_password")
    @classmethod
    def normalize_gmail_app_password(cls, value: str) -> str:
        """去掉应用专用密码里的全部空白。

        Google 界面上按 xxxx xxxx xxxx xxxx 分组显示，直接粘贴会带空格，
        而 IMAP 只接受连续 16 位，因此照抄就会登录失败。
        """
        # Gmail 应用专用密码显示时带空格（xxxx xxxx xxxx xxxx），
        # 复制粘贴进来会带空格，IMAP 登录不接受，这里统一去掉所有空白。
        return "".join(value.split())

    @field_validator("temp_mail_webhook_secret")
    @classmethod
    def validate_temp_mail_webhook_secret(cls, value: str) -> str:
        """webhook 密钥最短 32 字符且不含空白，避免弱密钥被人猜中往库里塞垃圾邮件。"""
        secret = value.strip()
        if secret and (len(secret) < 32 or any(character.isspace() for character in secret)):
            raise ValueError("TEMP_MAIL_WEBHOOK_SECRET must be at least 32 non-whitespace characters")
        return secret

    @model_validator(mode="after")
    def validate_login_email_protection(self) -> Settings:
        """邮箱保护开启时，后端凭据必须齐全且域名路由自洽。

        这里把三类错误都拦在启动阶段：缺域名、凭据配了一半、路由表引用了
        不在别名列表里的域名。运行时再发现这些问题，表现会是"登录时验证码
        永远等不到"，排查成本高得多。
        """
        if self.login_email_protection_enabled:
            if not self.login_email_alias_domains_raw:
                raise ValueError(
                    "login email protection is enabled but missing: LOGIN_EMAIL_ALIAS_DOMAINS"
                )
            gmail_values = (
                self.login_email_gmail_username,
                self.login_email_gmail_app_password,
            )
            gmail_configured = all(gmail_values)
            if any(gmail_values) and not gmail_configured:
                raise ValueError(
                    "LOGIN_EMAIL_GMAIL_USERNAME and LOGIN_EMAIL_GMAIL_APP_PASSWORD "
                    "must be configured together"
                )
            cloudflare_configured = bool(self.temp_mail_webhook_secret)
            if not gmail_configured and not cloudflare_configured:
                raise ValueError(
                    "login email protection requires Gmail credentials or "
                    "TEMP_MAIL_WEBHOOK_SECRET"
                )
            domains = set(self.login_email_alias_domains)
            for domain, backend in self.login_email_domain_backends.items():
                if domain not in domains:
                    raise ValueError(
                        f"LOGIN_EMAIL_DOMAIN_BACKENDS contains a domain not listed in "
                        f"LOGIN_EMAIL_ALIAS_DOMAINS: {domain}"
                    )
                if backend == "gmail" and not gmail_configured:
                    raise ValueError(f"Gmail credentials are required for domain: {domain}")
                if backend == "cloudflare" and not cloudflare_configured:
                    raise ValueError(f"TEMP_MAIL_WEBHOOK_SECRET is required for domain: {domain}")
        return self

    @cached_property
    def sync_database_url(self) -> str:
        """去掉 asyncpg 驱动的同一连接串，给备份等需要同步连接的地方用。"""
        return self.database_url.replace("+asyncpg", "")

    @cached_property
    def bot_user_id(self) -> int:
        """Bot 自己的数字 ID，从 token 前缀取。token 格式非法时返回 0 而不是抛异常。

        调用方用它判断"这条消息是不是 Bot 自己发的"，0 只是让判断永不命中，
        不影响其他功能。
        """
        try:
            return int(self.bot_token.split(":", 1)[0])
        except (TypeError, ValueError, IndexError):
            return 0

    @cached_property
    def login_email_alias_domains(self) -> tuple[str, ...]:
        """环境变量里配置的邮箱域名列表，按逗号切分并丢掉空项。

        这是运行时设置缺失时的兜底来源；用它缓存结果是因为
        每次换绑都会问到，而环境变量本身不会变。
        """
        return tuple(item for item in self.login_email_alias_domains_raw.split(",") if item)

    @property
    def login_email_domain_backends(self) -> dict[str, str]:
        """环境变量里配置的 域名→后端 映射，同样是运行时设置的兜底。"""
        return parse_domain_backends(self.login_email_domain_backends_raw)

    @property
    def configured_login_email_backends(self) -> frozenset[str]:
        """当前真正可用的后端集合，由凭据是否齐全推导，不由配置开关决定。"""
        backends: set[str] = set()
        if self.temp_mail_webhook_secret:
            backends.add("cloudflare")
        if self.login_email_gmail_username and self.login_email_gmail_app_password:
            backends.add("gmail")
        return frozenset(backends)

    @property
    def default_login_email_backend(self) -> str:
        """未在路由表里显式指定后端时用哪个。cloudflare 优先，因为它不占用 IMAP 连接。"""
        if "cloudflare" in self.configured_login_email_backends:
            return "cloudflare"
        return "gmail"


settings = Settings()
