# -*- coding: utf-8 -*-
"""
Telegram control bot untuk Free Fire squad bot.
Berjalan berdampingan dengan web dashboard. Baca/tulis `bot_state` dari dashboard_server.

Env vars:
  TELEGRAM_BOT_TOKEN  — token dari @BotFather (wajib)
  TELEGRAM_ADMIN_IDS  — comma-separated Telegram user IDs yang boleh kontrol (opsional;
                        kalau kosong = SEMUA orang bisa kontrol — bahaya, sebaiknya isi!)
"""

import asyncio
import logging
import os
import time
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from dashboard_server import (
    bot_state,
    _read_json_list,
    _write_json_atomic,
    _run_automation,
    _archive_accounts,
    _banned_snapshot,
)

log = logging.getLogger("telegram_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_raw_admins = os.getenv("TELEGRAM_ADMIN_IDS", "").strip()
ADMIN_IDS: set = {int(x) for x in _raw_admins.split(",") if x.strip().isdigit()}

_bot: Optional[Bot] = None
_dp = Dispatcher()          # handler didaftarkan di level modul, bukan di fungsi start


# ─── helper ────────────────────────────────────────────────────────────
def _is_admin(uid: int) -> bool:
    return (not ADMIN_IDS) or (uid in ADMIN_IDS)


def _fmt_dur(sec) -> str:
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h: return f"{h}h {m}m"
    if m: return f"{m}m {s}s"
    return f"{s}s"


def _icon(status: str) -> str:
    st = str(status or "").upper()
    return {
        "ONLINE": "🟢", "IN_MATCH": "🔵",
        "RE_LOGIN": "🟡", "CONNECTING": "🟡",
        "PAUSED": "⏸", "BANNED": "⛔", "ERROR": "🔴",
    }.get(st, "⚪")


async def _guard(message: Message) -> bool:
    if not message.from_user:
        return False
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Anda tidak punya izin menjalankan perintah ini.")
        return False
    return True


# ─── /start /help ──────────────────────────────────────────────────────
HELP_TEXT = (
    "🤖 <b>Free Fire Squad Bot — Kontrol Telegram</b>\n\n"
    "<b>Monitor</b>\n"
    "/stats — Statistik global\n"
    "/accounts [n] — Daftar akun (default 15)\n"
    "/squads — Status per squad\n"
    "/logs [n] — Log terbaru (default 20)\n"
    "/banned — Akun banned\n"
    "/guild — Info guild aktif\n\n"
    "<b>Kontrol</b>\n"
    "/pause &lt;uid&gt; — Jeda akun\n"
    "/resume &lt;uid&gt; — Lanjutkan akun\n"
    "/pauseall — Jeda semua\n"
    "/resumeall — Lanjutkan semua\n"
    "/restart &lt;uid&gt; — Refresh credentials\n\n"
    "<b>Manajemen akun</b>\n"
    "/add &lt;uid&gt; &lt;password&gt; — Tambah akun guest\n"
    "/addtoken &lt;token&gt; — Tambah akun via access token\n"
    "/delete &lt;uid&gt; — Hapus akun\n\n"
    "<b>Otomasi</b>\n"
    "/glory &lt;guild_id&gt; &lt;region&gt; [total] [threads]\n"
    "contoh: <code>/glory 3034881538 IND 5 5</code>\n"
)


@_dp.message(Command("start"))
@_dp.message(Command("help"))
async def cmd_help(message: Message):
    if not await _guard(message): return
    await message.answer(HELP_TEXT, parse_mode="HTML")


# ─── /stats ────────────────────────────────────────────────────────────
@_dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await _guard(message): return
    accs = list(bot_state.accounts.values())
    online = sum(1 for a in accs if a.get("status") in ("ONLINE", "IN_MATCH"))
    active = sum(int(a.get("active_matches", 0) or 0) for a in accs)
    uptime = int(time.time() - bot_state.start_time)
    exp_h = int((bot_state.total_gained_exp / uptime) * 3600) if uptime else 0

    rl = bot_state.relogin_events or {}
    rl_ok = sum(e.get("ok", 0) for e in rl.values())
    rl_fail = sum(e.get("fail", 0) for e in rl.values())

    banned = _banned_snapshot()

    await message.answer(
        "📊 <b>Statistik Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Uptime      : <b>{_fmt_dur(uptime)}</b>\n"
        f"👥 Akun        : <b>{len(accs)}</b> (online {online})\n"
        f"🎮 Match aktif : <b>{active}</b>\n"
        f"✅ Match total : <b>{bot_state.total_matches}</b> (started {bot_state.total_matches_started})\n"
        f"⭐ EXP gained  : <b>{bot_state.total_gained_exp:,}</b>\n"
        f"📈 EXP/jam     : <b>{exp_h:,}</b>\n"
        f"🔄 Re-login    : {rl_ok} OK / {rl_fail} FAIL\n"
        f"⛔ Banned      : {len(banned)}",
        parse_mode="HTML",
    )


# ─── /accounts ─────────────────────────────────────────────────────────
@_dp.message(Command("accounts"))
async def cmd_accounts(message: Message, command: CommandObject):
    if not await _guard(message): return
    try:
        n = int(command.args) if command.args else 15
    except Exception:
        n = 15
    n = max(1, min(30, n))

    accs = sorted(bot_state.accounts.values(),
                  key=lambda a: a.get("gained_exp", 0), reverse=True)
    if not accs:
        await message.answer("Belum ada akun online.")
        return

    lines = [f"👥 <b>Akun ({len(accs)})</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for a in accs[:n]:
        lines.append(
            f"{_icon(a.get('status'))} <code>{a.get('uid')}</code> "
            f"{a.get('nickname','?')} Lv{a.get('level','?')} "
            f"+{int(a.get('gained_exp',0) or 0):,}"
        )
    if len(accs) > n:
        lines.append(f"\n<i>...dan {len(accs)-n} akun lainnya</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── /squads ───────────────────────────────────────────────────────────
@_dp.message(Command("squads"))
async def cmd_squads(message: Message):
    if not await _guard(message): return
    sq = bot_state.squad_info or {}
    if not sq:
        await message.answer("Info squad belum tersedia.")
        return
    lines = ["🛡 <b>Status Squad</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for k in sorted(sq.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        info = sq[k]
        playing = bool(info.get("playing"))
        reg = int(info.get("registered", 0) or 0)
        size = int(info.get("size", 4) or 4)
        icon = "🎮" if playing else ("⏳" if reg < size else "🤝")
        stage = info.get("stage") or ""
        idle = int(info.get("idle_for", 0) or 0)
        line = f"{icon} <b>Squad {k}</b> — {reg}/{size}"
        if not playing and idle:
            line += f" (idle {_fmt_dur(idle)})"
        if stage:
            line += f"\n   <i>{stage}</i>"
        lines.append(line)
    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── /logs ─────────────────────────────────────────────────────────────
@_dp.message(Command("logs"))
async def cmd_logs(message: Message, command: CommandObject):
    if not await _guard(message): return
    try:
        n = int(command.args) if command.args else 20
    except Exception:
        n = 20
    n = max(1, min(50, n))
    logs = bot_state.logs[-n:]
    if not logs:
        await message.answer("Belum ada log.")
        return
    icons = {"success": "✅", "error": "❌", "warning": "⚠", "info": "ℹ"}
    lines = []
    for e in logs:
        ic = icons.get(e.get("level", "info"), "•")
        lines.append(f"{ic} <code>{e.get('time','')}</code> {e.get('message','')}")
    body = "\n".join(lines)
    if len(body) > 3800:
        body = body[-3800:]
    await message.answer(body, parse_mode="HTML")


# ─── /banned ───────────────────────────────────────────────────────────
@_dp.message(Command("banned"))
async def cmd_banned(message: Message):
    if not await _guard(message): return
    bn = _banned_snapshot()
    if not bn:
        await message.answer("✅ Tidak ada akun banned.")
        return
    lines = [f"⛔ <b>Banned ({len(bn)})</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for b in bn[:20]:
        lines.append(f"• <code>{b.get('key','?')}</code> — {b.get('reason','?')}")
    if len(bn) > 20:
        lines.append(f"...dan {len(bn)-20} lainnya")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── /guild ────────────────────────────────────────────────────────────
@_dp.message(Command("guild"))
async def cmd_guild(message: Message):
    if not await _guard(message): return
    g = bot_state.guild_config or {}
    if not g:
        await message.answer("Belum ada guild yang dikonfigurasi. Gunakan /glory.")
        return
    live = bot_state.guild_live or {}
    lines = [
        "🛡 <b>Info Guild</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Nama   : <b>{g.get('guild_name','?')}</b>",
        f"ID     : <code>{g.get('guild_id','?')}</code>",
        f"Region : <b>{g.get('region','?')}</b>",
        f"Level  : <b>{g.get('guild_level','?')}</b>",
    ]
    if live and live.get("glory"):
        lines += [
            f"Glory  : <b>{int(live['glory']):,}</b>",
            f"+ Run  : {int(live.get('gained_session',0)):+,}",
            f"+ Total: {int(live.get('gained_total',0)):+,}",
            f"Members: {live.get('members_online','?')}/{live.get('total_members','?')}",
        ]
    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── /pause /resume /pauseall /resumeall ───────────────────────────────
@_dp.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject):
    if not await _guard(message): return
    if not command.args:
        await message.answer("Format: /pause <uid>"); return
    uid = command.args.strip()
    if not bot_state.is_paused(uid):
        bot_state.toggle_pause(uid)
    await message.answer(f"⏸ Akun <code>{uid}</code> dijeda.", parse_mode="HTML")


@_dp.message(Command("resume"))
async def cmd_resume(message: Message, command: CommandObject):
    if not await _guard(message): return
    if not command.args:
        await message.answer("Format: /resume <uid>"); return
    uid = command.args.strip()
    if bot_state.is_paused(uid):
        bot_state.toggle_pause(uid)
    await message.answer(f"▶ Akun <code>{uid}</code> dilanjutkan.", parse_mode="HTML")


@_dp.message(Command("pauseall"))
async def cmd_pauseall(message: Message):
    if not await _guard(message): return
    for uid in list(bot_state.accounts.keys()):
        if not bot_state.is_paused(uid):
            bot_state.toggle_pause(uid)
    await message.answer("⏸ Semua akun dijeda.")


@_dp.message(Command("resumeall"))
async def cmd_resumeall(message: Message):
    if not await _guard(message): return
    for uid in list(bot_state.accounts.keys()):
        if bot_state.is_paused(uid):
            bot_state.toggle_pause(uid)
    await message.answer("▶ Semua akun dilanjutkan.")


# ─── /add /addtoken /delete /restart ───────────────────────────────────
@_dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    if not await _guard(message): return
    if not command.args or len(command.args.split()) < 2:
        await message.answer("Format: /add <uid> <password>"); return
    parts = command.args.split()
    uid, pwd = parts[0], parts[1]

    existing = _read_json_list("accounts.json")
    existing = [x for x in existing if str(x.get("uid", "")) != uid]
    existing.append({"uid": uid, "password": pwd})
    _write_json_atomic("accounts.json", existing)
    _archive_accounts()

    cb = bot_state.refresh_callbacks.get("on_account_added")
    if cb and bot_state.glory_started:
        asyncio.create_task(cb({"uid": uid, "password": pwd}))
        await message.answer(f"✅ Akun <code>{uid}</code> ditambahkan dan worker dimulai.",
                             parse_mode="HTML")
    else:
        await message.answer(
            f"✅ Akun <code>{uid}</code> ditambahkan. Glory belum jalan — start dulu.",
            parse_mode="HTML",
        )


@_dp.message(Command("addtoken"))
async def cmd_addtoken(message: Message, command: CommandObject):
    if not await _guard(message): return
    if not command.args:
        await message.answer("Format: /addtoken <access_token>"); return
    token = command.args.strip()

    existing = _read_json_list("accounts.json")
    existing = [x for x in existing if x.get("token", "") != token]
    existing.append({"token": token})
    _write_json_atomic("accounts.json", existing)
    _archive_accounts()

    cb = bot_state.refresh_callbacks.get("on_account_added")
    if cb and bot_state.glory_started:
        asyncio.create_task(cb({"token": token}))
        await message.answer("✅ Token akun ditambahkan dan worker dimulai.")
    else:
        await message.answer("✅ Token akun ditambahkan. Glory belum jalan — start dulu.")


@_dp.message(Command("delete"))
async def cmd_delete(message: Message, command: CommandObject):
    if not await _guard(message): return
    if not command.args:
        await message.answer("Format: /delete <uid>"); return
    uid = command.args.strip()

    existing = _read_json_list("accounts.json")
    new = [x for x in existing if str(x.get("uid", "")) != uid]
    _write_json_atomic("accounts.json", new)

    bot_state.accounts.pop(uid, None)
    bot_state.account_credentials.pop(uid, None)
    w = bot_state.account_workers.pop(uid, None)
    if w:
        w.cancel()
    bot_state.close_writers_for_account(uid)
    await message.answer(f"🗑 Akun <code>{uid}</code> dihapus.", parse_mode="HTML")


@_dp.message(Command("restart"))
async def cmd_restart(message: Message, command: CommandObject):
    if not await _guard(message): return
    if not command.args:
        await message.answer("Format: /restart <uid>"); return
    uid = command.args.strip()
    cb = bot_state.refresh_callbacks.get("on_refresh_account")
    if cb:
        asyncio.create_task(cb(uid))
        await message.answer(f"🔄 Refresh credentials untuk <code>{uid}</code> dipicu.",
                             parse_mode="HTML")
    else:
        await message.answer("⚠ Callback refresh belum terdaftar.")


# ─── /glory ────────────────────────────────────────────────────────────
@_dp.message(Command("glory"))
async def cmd_glory(message: Message, command: CommandObject):
    if not await _guard(message): return
    if not command.args or len(command.args.split()) < 2:
        await message.answer(
            "Format: /glory <guild_id> <region> [total] [threads]\n"
            "contoh: <code>/glory 3034881538 IND 5 5</code>",
            parse_mode="HTML",
        )
        return
    parts = command.args.split()
    gid, region = parts[0], parts[1].upper()
    try: total = int(parts[2]) if len(parts) > 2 else 0
    except Exception: total = 0
    try: threads = int(parts[3]) if len(parts) > 3 else 5
    except Exception: threads = 5
    total = max(0, min(500, total))
    threads = max(1, min(30, threads))

    if bot_state.automation.get("running"):
        await message.answer("⚠ Otomasi sedang berjalan. Tunggu selesai dulu.")
        return

    asyncio.create_task(_run_automation(gid, region, total, threads,
                                        fresh_pool=(total > 0)))
    await message.answer(
        "🚀 <b>Otomasi dimulai</b>\n"
        f"Guild: <code>{gid}</code> | Region: <b>{region}</b>\n"
        f"Akun baru: <b>{total}</b> | Threads: <b>{threads}</b>\n\n"
        "Pantau progres via /stats, /logs, atau dashboard web.",
        parse_mode="HTML",
    )


# ─── public API ────────────────────────────────────────────────────────
async def start_telegram_bot() -> Optional[asyncio.Task]:
    """Mulai polling Telegram sebagai background task. Panggil dari app.py::main()."""
    global _bot
    if not BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN kosong — bot Telegram tidak dijalankan.")
        return None
    try:
        _bot = Bot(token=BOT_TOKEN)
        me = await _bot.get_me()
        log.info(f"Telegram bot siap: @{me.username}")
        # Notif startup ke admin (opsional, diabaikan kalau gagal)
        for admin_id in ADMIN_IDS:
            try:
                await _bot.send_message(
                    admin_id,
                    "✅ <b>Bot Free Fire online.</b>\nKetik /help untuk daftar perintah.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return asyncio.create_task(
            _dp.start_polling(_bot, allowed_updates=["message"])
        )
    except Exception as e:
        log.error(f"Start Telegram bot gagal: {e}")
        return None
