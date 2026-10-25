import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from tradebot.config import load_config
from tradebot.monitoring import health
from tradebot.monitoring.bot import Router, Services
from tradebot.monitoring.telegram import TelegramAPI, TelegramNotifier
from tradebot.risk.state import HaltKind

ROOT = Path(__file__).resolve().parents[1]
OWNER, STRANGER = 111, 999
T0 = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)


class FakeAPI:
    def __init__(self):
        self.sent, self.edits, self.deleted = [], [], []

    def send(self, chat, text, buttons=None):
        self.sent.append((chat, text, buttons))
        return len(self.sent)

    def edit(self, chat, mid, text):
        self.edits.append((chat, mid, text))

    def answer_callback(self, cid, text=""):
        pass

    def _call(self, method, **kw):
        self.deleted.append(kw)


class FakeProposals:
    def __init__(self):
        self.calls = []
        p = SimpleNamespace(id="abc", contracts=8, version=2)
        self.store = SimpleNamespace(get=lambda pid: (p, None), pending=list)

    def _out(self, ok=True, msg="done"):
        return SimpleNamespace(ok=ok, message=msg, reasons=[], proposal=None, fresh=None)

    def approve(self, pid, user):
        self.calls.append(("approve", pid, user))
        return self._out(msg=f"#{pid} executed")

    def reject(self, pid, user, note=""):
        self.calls.append(("reject", pid, user))
        return self._out()

    def modify(self, pid, user, contracts=None, stop=None, target=None):
        self.calls.append(("modify", pid, contracts, stop, target))
        return self._out(ok=False, msg="modified")

    def expire_due(self):
        return []


class FakeRiskStore:
    def __init__(self):
        self.halts = []

    def halt(self, kind, reason, until=None, at=None):
        self.halts.append(kind)

    def state(self):
        return SimpleNamespace(equity_inr=100_000, day_pnl_pct=0.0, drawdown_pct=0.0, peak_equity_inr=100_000)


class FakeRisk:
    def __init__(self):
        self.store = FakeRiskStore()
        self.killed = None
        self.resumed = []

    def kill_switch(self, reason, by):
        self.killed = by

    def resume(self, by, kinds=None):
        self.resumed.append(kinds)
        return 1


def make(live=False, pin="482916"):
    cfg, secrets = load_config(ROOT / "config", env_file=None)
    cfg = cfg.model_copy(deep=True)
    cfg.telegram.allowed_user_ids = [OWNER]
    if live:
        cfg.mode = cfg.mode.__class__("live")
    secrets = secrets.model_copy(update={"telegram_approval_pin": SecretStr(pin) if pin else None})
    api, props, risk = FakeAPI(), FakeProposals(), FakeRisk()
    flattened = []
    positions = SimpleNamespace(
        answer=lambda rid, a, u: f"{a} done", daily_review=lambda: "review",
        e=SimpleNamespace(flatten_all=lambda why: flattened.append(why) or ["BTCUSD: closed"],
                          store=SimpleNamespace(requests=list)))
    clock = SimpleNamespace(t=T0)
    r = Router(Services(cfg, secrets, props, positions, risk, None, lambda: "ok", lambda: "sum",
                        lambda: "dig"), api, TelegramNotifier(api, [OWNER]), lambda: clock.t)
    return r, api, props, risk, flattened, clock


def msg(uid, text, mid=5):
    return {"message": {"from": {"id": uid}, "chat": {"id": uid}, "text": text, "message_id": mid}}


def btn(uid, data):
    return {"callback_query": {"id": "q", "from": {"id": uid}, "message": {"chat": {"id": uid}}, "data": data}}


def test_strangers_get_nothing():
    r, api, props, *_ = make()
    r.handle(msg(STRANGER, "/killswitch"))
    r.handle(btn(STRANGER, "ap:abc"))
    assert api.sent == [] and props.calls == []


def test_paper_approve_reject_buttons():
    r, api, props, *_ = make()
    r.handle(btn(OWNER, "ap:abc"))
    r.handle(btn(OWNER, "rj:abc"))
    assert props.calls == [("approve", "abc", str(OWNER)), ("reject", "abc", str(OWNER))]
    assert "executed" in api.sent[0][1]


def test_live_approve_needs_pin_with_lockout():
    r, api, props, _, _, clock = make(live=True)
    r.handle(btn(OWNER, "ap:abc"))
    assert "PIN" in api.sent[-1][1] and props.calls == []
    for _ in range(3):
        r.handle(btn(OWNER, "ap:abc"))
        r.handle(msg(OWNER, "000000"))
    assert props.calls == [] and "Wrong PIN (3/3)" in api.sent[-1][1]
    r.handle(btn(OWNER, "ap:abc"))
    r.handle(msg(OWNER, "482916"))
    assert props.calls == [] and "locked" in api.sent[-1][1]        # right PIN, still locked
    clock.t += timedelta(minutes=16)
    r.handle(btn(OWNER, "ap:abc"))
    r.handle(msg(OWNER, "482916", mid=77))
    assert props.calls == [("approve", "abc", str(OWNER))]
    assert {"chat_id": OWNER, "message_id": 77} in api.deleted        # PIN message wiped


def test_pin_window_expires():
    r, api, props, _, _, clock = make(live=True)
    r.handle(btn(OWNER, "ap:abc"))
    clock.t += timedelta(minutes=3)
    r.handle(msg(OWNER, "482916"))
    assert props.calls == [] and "expired" in api.sent[-1][1]


def test_killswitch_needs_confirm_tap():
    r, api, _, risk, flattened, _ = make()
    r.handle(msg(OWNER, "/killswitch"))
    assert risk.killed is None and api.sent[-1][2][0][0][1] == "kc:"
    r.handle(btn(OWNER, "kc:"))
    assert risk.killed == str(OWNER) and flattened and "KILL SWITCH ON" in api.sent[-1][1]


def test_pause_resume_and_modify_parsing():
    r, _api, props, risk, *_ = make()
    r.handle(msg(OWNER, "/pause"))
    assert risk.store.halts == [HaltKind.PAUSE]
    r.handle(msg(OWNER, "/resume"))
    assert risk.resumed == [{HaltKind.PAUSE, HaltKind.COOLDOWN}]
    r.handle(msg(OWNER, "/modify abc size=3 sl=80500.5 tp=84000"))
    assert props.calls[-1] == ("modify", "abc", 3, 80500.5, 84000.0)
    r.handle(btn(OWNER, "mh:abc"))
    assert props.calls[-1][:3] == ("modify", "abc", 4)                   # half of 8


def test_notifier_buttons_and_finalize():
    api = FakeAPI()
    n = TelegramNotifier(api, [OWNER])
    n.proposal(SimpleNamespace(id="abc"), "card")
    datas = [b[1] for row in api.sent[0][2] for b in row]
    assert datas == ["ap:abc", "rj:abc", "md:abc"]
    n.finalize("abc", "EXECUTED")
    assert api.edits == [(OWNER, 1, "EXECUTED")] and n.sent == {}


def test_telegram_api_payload():
    seen = []

    def handler(req):
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    api = TelegramAPI("T0KEN", transport=httpx.MockTransport(handler))
    assert api.send(1, "hi", [[("A", "a:1")]]) == 42
    path, body = seen[0]
    assert path == "/botT0KEN/sendMessage"
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "A", "callback_data": "a:1"}]]}


def test_watchdog_alerts_once_and_recovers(tmp_path):
    cfg, _ = load_config(ROOT / "config", env_file=None)
    cfg = cfg.model_copy(deep=True)
    cfg.monitoring.heartbeat_file = tmp_path / "hb.json"
    sent = []
    ok, _ = health.watchdog(cfg, sent.append)
    assert not ok and len(sent) == 1
    health.watchdog(cfg, sent.append)
    assert len(sent) == 1                                     # no spam during the outage
    health.write_heartbeat(cfg.monitoring.heartbeat_file, mode="paper")
    ok, _ = health.watchdog(cfg, sent.append)
    assert ok and "back" in sent[-1]


def test_next_daily():
    t = health.next_daily("21:30", datetime(2026, 10, 16, 15, 0, tzinfo=UTC))   # 20:30 IST
    assert t == datetime(2026, 10, 16, 16, 0, tzinfo=UTC)
    t2 = health.next_daily("21:30", datetime(2026, 10, 16, 16, 30, tzinfo=UTC))
    assert t2 - t == timedelta(days=1)


@pytest.mark.parametrize("live", [False])
def test_status_and_help(live):
    r, api, *_ = make(live)
    r.handle(msg(OWNER, "/help"))
    assert "/killswitch" in api.sent[-1][1]
    r.handle(msg(OWNER, "/nonsense"))
    assert "Unknown command" in api.sent[-1][1]
