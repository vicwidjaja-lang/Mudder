#!/usr/bin/env python3
"""
Automated character creation for dsl-mud.org:4000.
Run once to create the character, then use main.py to play.
"""

import asyncio
import sys
import time

# Telnet constants
IAC  = 255
WILL = 251
WONT = 252
DO   = 253
DONT = 254
SB   = 250
SE   = 240
ECHO = 1

# ── Credentials ──────────────────────────────────────────────────────────────
CHAR_NAME     = "Varek"
CHAR_PASSWORD = "gr1mBlade"
# ─────────────────────────────────────────────────────────────────────────────

TRANSCRIPT = []


def log(msg: str) -> None:
    TRANSCRIPT.append(msg)
    sys.stdout.write(msg)
    sys.stdout.flush()


class Session:
    def __init__(self, host: str, port: int):
        self.host   = host
        self.port   = port
        self.reader = None
        self.writer = None

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=15
        )

    async def close(self) -> None:
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    def _strip(self, data: bytes) -> str:
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd == IAC:
                    out.append(255); i += 2
                elif cmd in (WILL, WONT, DO, DONT) and i + 2 < len(data):
                    opt = data[i + 2]
                    if   cmd == DO:   self.writer.write(bytes([IAC, WONT, opt]))
                    elif cmd == WILL:
                        resp = DO if opt == ECHO else DONT
                        self.writer.write(bytes([IAC, resp, opt]))
                    i += 3
                elif cmd == SB:
                    end = data.find(bytes([IAC, SE]), i + 2)
                    i = (end + 2) if end >= 0 else len(data)
                else:
                    i += 2
            elif b == IAC:
                i += 1
            else:
                out.append(b); i += 1
        return out.decode("utf-8", errors="replace")

    async def read(self, secs: float = 3.0) -> str:
        """Accumulate input for up to `secs` seconds."""
        text = ""
        deadline = asyncio.get_event_loop().time() + secs
        while True:
            rem = deadline - asyncio.get_event_loop().time()
            if rem <= 0:
                break
            try:
                chunk = await asyncio.wait_for(self.reader.read(4096),
                                               timeout=min(rem, 0.4))
                if not chunk:
                    break
                decoded = self._strip(chunk)
                text += decoded
                log(decoded)
            except asyncio.TimeoutError:
                # If we've seen some text and it looks like a prompt, stop early
                t = text.lower()
                if any(t.rstrip().endswith(c) for c in (":", "?", ">")):
                    break
                if rem < 0.5:
                    break
        return text

    async def send(self, text: str, pause: float = 0.9) -> None:
        await asyncio.sleep(pause)
        log(f"\n>>> {text!r}\n")
        self.writer.write((text + "\r\n").encode("utf-8", errors="replace"))
        await self.writer.drain()


async def create(name: str, password: str) -> bool:
    s = Session("dsl-mud.org", 4000)
    await s.connect()
    log("[connected]\n")

    # ── Step 1: initial greeting / name prompt ────────────────────────────
    txt = await s.read(6)

    # If a MOTD/welcome screen, wait for it to settle
    if len(txt) > 200:
        txt += await s.read(3)

    # Send name
    await s.send(name)
    txt = await s.read(5)

    # ── Step 2: name-taken or confirmation ───────────────────────────────
    tl = txt.lower()
    if "already exists" in tl or "taken" in tl or "pick another" in tl:
        log("\n[Name taken — try a different name]\n")
        await s.close()
        return False

    if "did i get" in tl or "(y/n)" in tl or "correct" in tl or "right" in tl:
        await s.send("y")
        txt = await s.read(4)
        tl = txt.lower()

    # ── Step 3: password ─────────────────────────────────────────────────
    if "password" in tl or "passwd" in tl:
        await s.send(password)
        txt = await s.read(4)
        tl = txt.lower()

    # Retype / confirm
    if "retype" in tl or "again" in tl or "confirm" in tl or "verify" in tl:
        await s.send(password)
        txt = await s.read(4)
        tl = txt.lower()

    # ── Step 4: sex / gender ─────────────────────────────────────────────
    if "sex" in tl or "gender" in tl or "(m/f)" in tl:
        await s.send("M")
        txt = await s.read(4)
        tl = txt.lower()

    # ── Step 5: race ─────────────────────────────────────────────────────
    if "race" in tl:
        # Look for "human" with a number, e.g.  "1) Human"
        human_num = None
        for line in txt.split("\n"):
            if "human" in line.lower():
                nums = [c for c in line if c.isdigit()]
                if nums:
                    human_num = nums[0]
                    break
        await s.send(human_num or "1")
        txt = await s.read(4)
        tl = txt.lower()

    # ── Step 6: class ────────────────────────────────────────────────────
    if "class" in tl or "profession" in tl or "warrior" in tl:
        warrior_num = None
        for line in txt.split("\n"):
            if "warrior" in line.lower():
                nums = [c for c in line if c.isdigit()]
                if nums:
                    warrior_num = nums[0]
                    break
        await s.send(warrior_num or "4")
        txt = await s.read(6)
        tl = txt.lower()

    # ── Step 7: alignment (if asked) ─────────────────────────────────────
    if "align" in tl or "neutral" in tl or "good" in tl and "evil" in tl:
        # Choose neutral for a warrior
        neutral_num = None
        for line in txt.split("\n"):
            if "neutral" in line.lower():
                nums = [c for c in line if c.isdigit()]
                if nums:
                    neutral_num = nums[0]
                    break
        await s.send(neutral_num or "2")
        txt = await s.read(5)
        tl = txt.lower()

    # ── Step 8: stat rolling (some MUDs ask) ─────────────────────────────
    for _ in range(5):
        tl = txt.lower()
        if "reroll" in tl or "roll" in tl or "accept" in tl or "keep" in tl:
            await s.send("y")   # accept / keep stats
            txt = await s.read(5)
        else:
            break

    # ── Step 9: read any remaining intro text ────────────────────────────
    await s.read(8)

    # Gentle quit
    await s.send("quit", pause=1.5)
    await s.read(3)

    await s.close()
    return True


async def main() -> None:
    name     = CHAR_NAME
    password = CHAR_PASSWORD

    log(f"\n=== Character Creation ===\n")
    log(f"Name:     {name}\n")
    log(f"Password: {password}\n\n")

    ok = await create(name, password)
    if ok:
        log(f"\n{'='*40}\n")
        log(f"Character created (or already exists and logged in).\n")
        log(f"USERNAME: {name}\n")
        log(f"PASSWORD: {password}\n")
        log(f"{'='*40}\n")
    else:
        log("\nCreation failed — check transcript above.\n")


if __name__ == "__main__":
    asyncio.run(main())
