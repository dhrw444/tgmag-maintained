"""aiogram FSM 状态组。

每个多步交互一个 StatesGroup，state 名对应流程里的下一步。这里只声明状态，
不含任何逻辑 —— 状态的推进和清理都在 handlers 里，因为同一个状态可能从多个
入口进入（命令、按钮、回调），集中声明反而更难追踪。

注意：FSM 状态存在 Dispatcher 的 storage 里，进程重启后全部丢失。
需要跨重启存活的流程（比如等邮件的换绑窗口）不能只靠这里，
见 login_email_protection 的事件表。
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class LoginFlow(StatesGroup):
    """手机号登录 Telegram 账号的会话状态。

    qr_wait 与 phone/code 并列：用户可以在扫码和验证码之间来回切，
    状态停在 qr_wait 表示正在等扫码而不是等验证码。
    """

    phone = State()
    code = State()
    qr_wait = State()
    password = State()


class ImportSessionFlow(StatesGroup):
    """导入已有 session 文件：先要手机号，再收文件本体。"""

    phone = State()
    session = State()


class ImportSessionsFlow(StatesGroup):
    """批量导入：一次性接收多行「手机号 + session」文本。"""

    payload = State()


class ExportSessionFlow(StatesGroup):
    """导出 session：等待用户在键盘上选择要导出哪个账号。"""

    selection = State()


class ProfileEditFlow(StatesGroup):
    """改资料（姓名/简介/用户名/头像）共用的单值输入状态。"""

    value = State()


class TwoFAEditFlow(StatesGroup):
    """设置或修改二级密码：先收密码，如果 Telegram 要求邮箱确认再收验证码。
    """

    value = State()
    email_code = State()


class LoginEmailDomainFlow(StatesGroup):
    """添加登录邮箱域名时等待用户输入域名。"""

    value = State()


class LoginEmailWindowFlow(StatesGroup):
    """修改保护窗口小时数时等待用户输入数字。"""

    value = State()


class LoginEmailEditFlow(StatesGroup):
    """手动改绑登录邮箱：先收目标邮箱，再收 Telegram 发来的验证码。
    """

    email = State()
    code = State()
