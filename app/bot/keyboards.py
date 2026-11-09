from __future__ import annotations

from aiogram.types import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from app.config import settings
from app.db.models import LoginEmailProtectionEvent, TgAccount


def main_menu() -> ReplyKeyboardMarkup:
    """主菜单回复键盘。常驻但可折叠（is_persistent=False），不占满屏幕。"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="系统状态"), KeyboardButton(text="账号管理")],
            [
                KeyboardButton(text="登录账号"),
                KeyboardButton(text="扫码登录"),
            ],
            [KeyboardButton(text="安全防护")],
        ],
        resize_keyboard=True,
        is_persistent=False,
        one_time_keyboard=False,
        input_field_placeholder="选择常用操作",
    )


def cancel_inline() -> InlineKeyboardMarkup:
    """任何多步流程的取消按钮。callback_data 固定为 flow:cancel。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="取消当前操作", callback_data="flow:cancel")]]
    )


def login_phone_panel() -> InlineKeyboardMarkup:
    """手机号登录面板，提供转二维码登录和取消两个出口。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="改用二维码登录", callback_data="login:qr")],
            [InlineKeyboardButton(text="取消当前操作", callback_data="flow:cancel")],
        ]
    )


def force_reply(placeholder: str) -> ForceReply:
    """强制回复框。selective=True 只对当前用户生效，不打扰群里其他人。"""
    return ForceReply(
        force_reply=True,
        input_field_placeholder=placeholder,
        selective=True,
    )


def mini_app_panel() -> InlineKeyboardMarkup | None:
    """Mini App 入口面板；未配置 mini_app_public_url 时返回 None，调用方要判空。"""
    mini_app_url = settings.mini_app_public_url.strip()
    if not mini_app_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="打开", web_app=WebAppInfo(url=mini_app_url))],
            [InlineKeyboardButton(text="返回", callback_data="nav:home")],
        ]
    )


def bot_menu_button() -> MenuButtonCommands | MenuButtonWebApp:
    """Bot 菜单按钮：配置了 Mini App 就用原生 Web App 启动器，否则退回命令列表。"""
    mini_app_url = settings.mini_app_public_url.strip()
    if not settings.mini_app_enabled or not mini_app_url:
        return MenuButtonCommands()
    return MenuButtonWebApp(
        text="打开",
        web_app=WebAppInfo(url=mini_app_url),
    )


def account_button_label(account: TgAccount) -> str:
    """账号按钮的显示文本，优先 user_id，其次 @username，最后掩码手机号。"""
    if account.user_id:
        identity = str(account.user_id)
    elif account.username:
        identity = f"@{account.username}"
    else:
        identity = account.phone_masked
    return f"#{account.id} · {identity}"


def accounts_panel(
    accounts: list[TgAccount],
    page: int = 1,
    pages: int = 1,
) -> InlineKeyboardMarkup:
    """账号列表分页面板。pages > 1 时才出现翻页行。"""
    rows: list[list[InlineKeyboardButton]] = []
    for account in accounts:
        rows.append(
            [
                InlineKeyboardButton(
                    text=account_button_label(account),
                    callback_data=f"acct:{account.id}:{page}",
                )
            ]
        )
    if pages > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    text="‹ 上一页",
                    callback_data=f"nav:accounts:{max(page - 1, 1)}",
                ),
                InlineKeyboardButton(
                    text=f"{page}/{pages}",
                    callback_data=f"nav:accounts:{page}",
                ),
                InlineKeyboardButton(
                    text="下一页 ›",
                    callback_data=f"nav:accounts:{min(page + 1, pages)}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="刷新", callback_data=f"nav:accounts:{page}"),
            InlineKeyboardButton(text="返回", callback_data="nav:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_actions_panel(account_id: int, accounts_page: int = 1) -> InlineKeyboardMarkup:
    """单账号操作面板，所有账号级动作的入口。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="重连", callback_data=f"acct_action:reconnect:{account_id}"
                ),
                InlineKeyboardButton(
                    text="SpamBot", callback_data=f"acct_action:spam:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="详细信息", callback_data=f"acct_action:detail:{account_id}"
                ),
                InlineKeyboardButton(
                    text="刷新检测", callback_data=f"acct_action:check_detail:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="拉取777000", callback_data=f"acct_action:service:{account_id}"
                ),
                InlineKeyboardButton(text="2FA", callback_data=f"acct_action:twofa:{account_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="隐私快照", callback_data=f"acct_action:privacy:{account_id}"
                ),
                InlineKeyboardButton(
                    text="资料设置", callback_data=f"acct_panel:profile:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="隐私设置", callback_data=f"acct_panel:privacy:{account_id}"
                ),
                InlineKeyboardButton(
                    text="2FA 设置", callback_data=f"acct_panel:twofa:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="配置登录邮箱", callback_data=f"loginemail:configure:{account_id}",
                ),
                InlineKeyboardButton(
                    text="登录邮箱保护",
                    callback_data=f"emailguard:account:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="导出 Session", callback_data=f"acct_action:export_session:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="删除账号", callback_data=f"acct_delete:ask:{account_id}:{accounts_page}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="返回列表", callback_data=f"nav:accounts:{accounts_page}"
                ),
            ],
        ]
    )


def account_delete_confirm_panel(
    account_id: int,
    accounts_page: int,
    *,
    final: bool = False,
) -> InlineKeyboardMarkup:
    """删除确认面板。final=False 是第一次确认，final=True 才是永久删除。"""
    action = "confirm" if final else "verify"
    label = "确认永久删除" if final else "继续删除"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"acct_delete:{action}:{account_id}:{accounts_page}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="取消并返回账号",
                    callback_data=f"acct:{account_id}:{accounts_page}",
                )
            ],
        ]
    )


def post_login_security_panel(account_id: int) -> InlineKeyboardMarkup:
    """登录成功后的提示面板，引导去设置 2FA。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="打开 2FA 设置",
                    callback_data=f"acct_panel:twofa:{account_id}",
                ),
                InlineKeyboardButton(
                    text="返回账号",
                    callback_data=f"acct:{account_id}",
                ),
            ]
        ]
    )


def profile_edit_panel(account_id: int) -> InlineKeyboardMarkup:
    """资料编辑入口。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="改姓名", callback_data=f"acct_edit:name:{account_id}"),
                InlineKeyboardButton(text="改简介", callback_data=f"acct_edit:bio:{account_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="改用户名", callback_data=f"acct_edit:username:{account_id}"
                ),
                InlineKeyboardButton(
                    text="头像设置", callback_data=f"acct_panel:avatar:{account_id}"
                ),
            ],
            [InlineKeyboardButton(text="返回账号", callback_data=f"acct:{account_id}")],
        ]
    )


def avatar_panel(account_id: int) -> InlineKeyboardMarkup:
    """头像设置入口：上传本地图片或从白名单随机取一张。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="上传图片", callback_data=f"acct_edit:avatar_upload:{account_id}"
                ),
                InlineKeyboardButton(
                    text="随机头像", callback_data=f"acct_action:avatar_random:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="返回资料", callback_data=f"acct_panel:profile:{account_id}"
                )
            ],
        ]
    )


def privacy_keys_panel(account_id: int) -> InlineKeyboardMarkup:
    """隐私项选择，每个按钮对应一个 Telegram 隐私 key。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="手机号", callback_data=f"privacy_key:phone:{account_id}"
                ),
                InlineKeyboardButton(
                    text="在线时间", callback_data=f"privacy_key:last_seen:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="头像", callback_data=f"privacy_key:profile_photo:{account_id}"
                ),
                InlineKeyboardButton(
                    text="转发来源", callback_data=f"privacy_key:forwards:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(text="通话", callback_data=f"privacy_key:calls:{account_id}"),
                InlineKeyboardButton(text="拉群", callback_data=f"privacy_key:groups:{account_id}"),
            ],
            [InlineKeyboardButton(text="返回账号", callback_data=f"acct:{account_id}")],
        ]
    )


def privacy_rules_panel(account_id: int, key_name: str) -> InlineKeyboardMarkup:
    """单个隐私项的可选值。key_name 会原样进 callback_data。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="所有人", callback_data=f"privacy_set:{key_name}:everybody:{account_id}"
                ),
                InlineKeyboardButton(
                    text="联系人", callback_data=f"privacy_set:{key_name}:contacts:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="没有人", callback_data=f"privacy_set:{key_name}:nobody:{account_id}"
                ),
                InlineKeyboardButton(text="返回", callback_data=f"acct_panel:privacy:{account_id}"),
            ],
        ]
    )


def twofa_panel(account_id: int) -> InlineKeyboardMarkup:
    """2FA 操作面板：查询、设置、修改、恢复邮箱、关闭。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="查询状态", callback_data=f"acct_action:twofa:{account_id}"
                ),
                InlineKeyboardButton(text="设置 2FA", callback_data=f"twofa_edit:set:{account_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="修改 2FA", callback_data=f"twofa_edit:change:{account_id}"
                ),
                InlineKeyboardButton(
                    text="配置 2FA 恢复邮箱", callback_data=f"twofa_edit:email:{account_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="关闭 2FA", callback_data=f"twofa_edit:disable:{account_id}"
                ),
                InlineKeyboardButton(text="返回账号", callback_data=f"acct:{account_id}"),
            ],
        ]
    )


def batch_panel() -> InlineKeyboardMarkup:
    """批量操作与模板入口。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="发送模板", callback_data="template:send"),
                InlineKeyboardButton(text="关注模板", callback_data="template:subscribe"),
            ],
            [
                InlineKeyboardButton(text="反应模板", callback_data="template:react"),
                InlineKeyboardButton(text="浏览模板", callback_data="template:view_post"),
            ],
            [
                InlineKeyboardButton(text="转发模板", callback_data="template:forward"),
                InlineKeyboardButton(text="导出 Session", callback_data="flow:export_session"),
            ],
            [InlineKeyboardButton(text="批量导入 Session", callback_data="flow:import_sessions")],
            [InlineKeyboardButton(text="返回", callback_data="nav:home")],
        ]
    )


def settings_panel() -> InlineKeyboardMarkup:
    """设置入口：白名单与速率。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="查看白名单", callback_data="settings:targets"),
                InlineKeyboardButton(text="查看速率", callback_data="settings:rate"),
            ],
            [
                InlineKeyboardButton(text="添加白名单模板", callback_data="template:target_add"),
                InlineKeyboardButton(text="设置速率模板", callback_data="template:rate_set"),
            ],
            [InlineKeyboardButton(text="返回", callback_data="nav:home")],
        ]
    )


def monitor_panel() -> InlineKeyboardMarkup:
    """监听控制面板。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="开启监听", callback_data="monitor:on"),
                InlineKeyboardButton(text="关闭监听", callback_data="monitor:off"),
            ],
            [
                InlineKeyboardButton(text="通知测试", callback_data="monitor:notify"),
                InlineKeyboardButton(text="系统状态", callback_data="nav:status"),
            ],
            [InlineKeyboardButton(text="安全防护", callback_data="nav:security")],
            [InlineKeyboardButton(text="返回", callback_data="nav:home")],
        ]
    )


def login_email_guard_panel() -> InlineKeyboardMarkup:
    """登录邮箱保护总面板。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="邮箱域名管理", callback_data="emailguard:domains"),
                InlineKeyboardButton(text="账号白名单", callback_data="emailguard:whitelist"),
            ],
            [
                InlineKeyboardButton(text="最近保护事件", callback_data="emailguard:events"),
                InlineKeyboardButton(text="检查邮件接收", callback_data="emailguard:testimap"),
            ],
            [
                InlineKeyboardButton(
                    text="检测整个安全防护链路", callback_data="emailguard:checkall"
                )
            ],
            [InlineKeyboardButton(text="刷新", callback_data="emailguard:open")],
            [InlineKeyboardButton(text="返回主菜单", callback_data="nav:home")],
        ]
    )


def login_email_domains_panel(
    domains: tuple[str, ...],
    selected_domain: str | None,
    domain_backends: dict[str, str] | None = None,
) -> InlineKeyboardMarkup:
    """域名列表。每行一个后端切换按钮加一个删除按钮，索引进 callback_data。

    用索引而不是域名本身：域名可能很长，而 callback_data 有 64 字节上限。
    """
    domain_backends = domain_backends or {}
    rows: list[list[InlineKeyboardButton]] = []
    for index, domain in enumerate(domains):
        marker = "✅" if domain == selected_domain else "○"
        backend = domain_backends.get(domain, "cloudflare")
        backend_text = "☁️ CF TempMail" if backend == "cloudflare" else "✉️ Gmail"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} @{domain}",
                    callback_data=f"emailguard:domain:{index}",
                ),
                InlineKeyboardButton(
                    text=backend_text,
                    callback_data=f"emailguard:backend:{index}",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"删除 @{domain}",
                    callback_data=f"emailguard:deleteask:{index}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="添加邮箱域名", callback_data="emailguard:add")])
    rows.append([InlineKeyboardButton(text="返回安全中心", callback_data="emailguard:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def login_email_whitelist_panel(
    accounts: list[TgAccount],
    whitelisted_ids: set[int],
) -> InlineKeyboardMarkup:
    """账号白名单开关。最多显示 30 个账号，避免按钮过多被 Telegram 拒收。"""
    rows: list[list[InlineKeyboardButton]] = []
    account_buttons: list[InlineKeyboardButton] = []
    for account in accounts[:30]:
        marker = "✅ 移出" if account.id in whitelisted_ids else "＋ 加入"
        account_buttons.append(
            InlineKeyboardButton(
                text=f"{marker} · #{account.id} {account.phone_masked}",
                callback_data=f"emailguard:white:{account.id}",
            )
        )
    rows.extend([[button] for button in account_buttons])
    rows.append([InlineKeyboardButton(text="返回安全中心", callback_data="emailguard:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def login_email_delete_confirm_panel(index: int) -> InlineKeyboardMarkup:
    """删除域名的二次确认。"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="确认删除",
                    callback_data=f"emailguard:delete:{index}",
                ),
                InlineKeyboardButton(text="取消", callback_data="emailguard:domains"),
            ]
        ]
    )


def login_email_events_panel(
    events: list[LoginEmailProtectionEvent],
) -> InlineKeyboardMarkup:
    """保护事件列表。status 未知时原样显示，便于发现新状态忘了加标签。"""
    labels = {
        "succeeded": "✅ 成功",
        "failed": "❌ 失败",
        "whitelisted": "🟦 白名单",
        "disabled": "⚪ 未启用",
        "cooldown": "⏳ 冷却",
        "waiting_window": "🕗 聚合中",
        "requesting": "🔄 请求中",
        "waiting_email": "📨 等待邮件",
        "interrupted": "⚠️ 已中断",
    }
    rows = [
        [
            InlineKeyboardButton(
                text=f"{labels.get(event.status, event.status)} · #{event.account_id} · 事件{event.id}",
                callback_data=f"emailguard:event:{event.id}",
            )
        ]
        for event in events
    ]
    rows.append([InlineKeyboardButton(text="返回安全中心", callback_data="emailguard:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def login_email_account_panel(account_id: int, whitelisted: bool) -> InlineKeyboardMarkup:
    """单账号保护设置：等待时间与白名单开关。"""
    label = "移出白名单" if whitelisted else "加入白名单"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="设置等待时间",
                    callback_data=f"emailguard:window:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"emailguard:accounttoggle:{account_id}",
                )
            ],
            [InlineKeyboardButton(text="返回账号", callback_data=f"acct:{account_id}")],
        ]
    )


def login_email_retry_panel(
    event_id: int,
    domains: tuple[str, ...],
    failed_domain: str | None,
) -> InlineKeyboardMarkup:
    """失败重试面板，列出可用域名并标出上次失败的那个。"""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'↻' if domain == failed_domain else '→'} 使用 @{domain}",
                callback_data=f"emailguard:retry:{event_id}:{index}",
            )
        ]
        for index, domain in enumerate(domains)
    ]
    rows.append([InlineKeyboardButton(text="返回保护设置", callback_data="emailguard:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
