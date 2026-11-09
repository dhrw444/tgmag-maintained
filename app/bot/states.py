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
    phone = State()
    code = State()
    qr_wait = State()
    password = State()


class ImportSessionFlow(StatesGroup):
    phone = State()
    session = State()


class ImportSessionsFlow(StatesGroup):
    payload = State()


class ExportSessionFlow(StatesGroup):
    selection = State()


class ProfileEditFlow(StatesGroup):
    value = State()


class TwoFAEditFlow(StatesGroup):
    value = State()
    email_code = State()


class LoginEmailDomainFlow(StatesGroup):
    value = State()


class LoginEmailWindowFlow(StatesGroup):
    value = State()


class LoginEmailEditFlow(StatesGroup):
    email = State()
    code = State()
