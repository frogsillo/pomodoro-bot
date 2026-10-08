"""Motor del temporizador Pomodoro."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord

from config import (
    LONG_BREAK_SECONDS, POMODOROS_PER_CYCLE,
    SHORT_BREAK_SECONDS, WORK_SECONDS,
)
from database import Database, iso_utc

log = logging.getLogger(__name__)

WORK = "work"
SHORT_BREAK = "short_break"
LONG_BREAK = "long_break"

PHASE_EMOJI = {WORK: "🍅", SHORT_BREAK: "☕", LONG_BREAK: "🛋️"}
PHASE_LABEL = {WORK: "TRABAJO", SHORT_BREAK: "DESCANSO", LONG_BREAK: "DESCANSO LARGO"}
PHASE_NAME = {WORK: "Trabajo", SHORT_BREAK: "Descanso corto", LONG_BREAK: "Descanso largo"}

COLOR_WORK = 0xE74C3C
COLOR_SHORT = 0x2ECC71
COLOR_LONG = 0x3498DB
COLOR_INFO = 0x5865F2
COLOR_ERROR = 0xED4245

UPDATE_INTERVAL = 5.0


def format_time(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Durations:
    work: int = WORK_SECONDS
    short_break: int = SHORT_BREAK_SECONDS
    long_break: int = LONG_BREAK_SECONDS
    pomodoros_per_cycle: int = POMODOROS_PER_CYCLE

    def for_phase(self, phase: str) -> int:
        return {WORK: self.work, SHORT_BREAK: self.short_break, LONG_BREAK: self.long_break}[phase]


def default_durations() -> Durations:
    return Durations()


class PomodoroSession:
    """Gestiona UNA sesión Pomodoro en UN servidor."""

    def __init__(
        self,
        *,
        bot: discord.Client,
        db: Database,
        guild: discord.Guild,
        owner_id: int,
        voice_client: discord.VoiceClient,
        voice_channel: discord.abc.Connectable,
        text_channel: discord.abc.Messageable,
        sound_path: Optional[Path],
        durations: Optional[Durations] = None,
        phase: str = WORK,
        pomodoro_count: int = 1,
        total_work_done: int = 0,
        session_started_at: Optional[datetime] = None,
        remaining: Optional[float] = None,
        paused: bool = False,
        deadline_monotonic: Optional[float] = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.guild = guild
        self.guild_id = guild.id
        self.owner_id = owner_id
        self.voice_client = voice_client
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.sound_path = Path(sound_path) if sound_path else None
        self.durations = durations or default_durations()

        self.phase = phase
        self.pomodoro_count = pomodoro_count
        self.total_work_done = total_work_done
        self.session_started_at = session_started_at or _utcnow()
        self.paused = paused
        self.stopped = False

        self._remaining = float(remaining if remaining is not None else self.durations.for_phase(phase))
        if paused:
            self._deadline: Optional[float] = None
        elif deadline_monotonic is not None:
            self._deadline = deadline_monotonic
        else:
            self._deadline = time.monotonic() + self._remaining

        self._loop_task: Optional[asyncio.Task] = None
        self._update_task: Optional[asyncio.Task] = None
        self._main_message: Optional[discord.Message] = None
        self._view: Optional[discord.ui.View] = None

    # ---------------- propiedades ----------------
    @property
    def remaining(self) -> float:
        if self.stopped:
            return 0.0
        if self.paused or self._deadline is None:
            return max(0.0, self._remaining)
        return max(0.0, self._deadline - time.monotonic())

    @property
    def is_running(self) -> bool:
        return not self.stopped

    # ---------------- control ----------------
    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = self.bot.loop.create_task(self._run_loop())
        if self._update_task is None or self._update_task.done():
            self._update_task = self.bot.loop.create_task(self._update_loop())

    def pause(self) -> bool:
        if self.stopped or self.paused:
            return False
        if self._deadline is not None:
            self._remaining = max(0.0, self._deadline - time.monotonic())
        self._deadline = None
        self.paused = True
        self._schedule_persist()
        return True

    def resume(self) -> bool:
        if self.stopped or not self.paused:
            return False
        self._deadline = time.monotonic() + self._remaining
        self.paused = False
        self._schedule_persist()
        return True

    async def stop(self, *, disconnect: bool = True, record: bool = True) -> None:
        if self.stopped:
            return
        self.stopped = True

        for t in (self._loop_task, self._update_task):
            if t is not None and not t.done() and t is not asyncio.current_task():
                t.cancel()

        # Persistencia final
        try:
            await self.db.delete_active_session(self.guild_id)
        except Exception:
            log.exception("No se pudo eliminar la sesión activa de la BD")

        if record and (self.total_work_done > 0 or self.pomodoro_count > 1):
            try:
                await self.db.record_session(
                    guild_id=self.guild_id,
                    user_id=self.owner_id,
                    started_at=self.session_started_at,
                    ended_at=_utcnow(),
                    pomodoros_completed=self.total_work_done,
                    work_seconds=self.total_work_done * self.durations.work,
                )
            except Exception:
                log.exception("No se pudo registrar la sesión finalizada")

        if disconnect and self.voice_client is not None:
            try:
                if self.voice_client.is_connected():
                    await self.voice_client.disconnect(force=True)
            except Exception:
                log.exception("Error al desconectar del canal de voz")

    def _schedule_persist(self) -> None:
        self.bot.loop.create_task(self._persist())

    async def _persist(self) -> None:
        if self.stopped:
            return
        try:
            await self.db.save_active_session({
                "guild_id": self.guild_id,
                "owner_id": self.owner_id,
                "voice_channel_id": getattr(self.voice_channel, "id", 0),
                "text_channel_id": getattr(self.text_channel, "id", 0),
                "phase": self.phase,
                "remaining_seconds": float(self.remaining),
                "deadline_at": iso_utc(_utcnow()) if self.paused else None,
                "paused": 1 if self.paused else 0,
                "pomodoro_count": self.pomodoro_count,
                "total_work_done": self.total_work_done,
                "session_started_at": iso_utc(self.session_started_at),
            })
        except Exception:
            log.exception("Error al persistir la sesión activa")

    # ---------------- bucle principal ----------------
    async def _run_loop(self) -> None:
        try:
            last_persist = 0.0
            while not self.stopped:
                if self.paused or self._deadline is None:
                    await asyncio.sleep(0.25)
                    continue

                now = time.monotonic()
                if now >= self._deadline:
                    self._remaining = 0.0
                    await self._advance_phase()
                    last_persist = 0.0
                    continue

                self._remaining = self._deadline - now
                if now - last_persist > 15.0:
                    last_persist = now
                    await self._persist()
                await asyncio.sleep(min(1.0, self._remaining))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error inesperado en el bucle Pomodoro")
            self.stopped = True
            await self.send(content="❌ La sesión Pomodoro se detuvo por un error interno.")

    async def _update_loop(self) -> None:
        try:
            while not self.stopped:
                await asyncio.sleep(UPDATE_INTERVAL)
                if self.stopped:
                    return
                await self._refresh_main_message()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error en el bucle de actualización de embed")

    # ---------------- transición de fase ----------------
    async def _advance_phase(self) -> None:
        finished = self.phase

        await self._play_sound()

        if finished == WORK:
            self.total_work_done += 1
            try:
                await self.db.record_pomodoro(self.owner_id, self.guild_id, self.durations.work)
            except Exception:
                log.exception("Error registrando pomodoro en la BD")

            if self.pomodoro_count >= self.durations.pomodoros_per_cycle:
                self.phase = LONG_BREAK
                self._remaining = float(self.durations.long_break)
                embed = discord.Embed(
                    title="🎉 ¡Completaste 4 Pomodoros!",
                    description=f"Comenzando **descanso largo de {self.durations.long_break // 60} minutos**.",
                    color=COLOR_LONG,
                )
            else:
                self.phase = SHORT_BREAK
                self._remaining = float(self.durations.short_break)
                embed = discord.Embed(
                    title="🔔 Terminó el período de trabajo.",
                    description=f"Comenzando **descanso de {self.durations.short_break // 60} minutos**.",
                    color=COLOR_SHORT,
                )
                embed.add_field(
                    name="Pomodoro completado",
                    value=f"{self.pomodoro_count}/{self.durations.pomodoros_per_cycle}",
                    inline=False,
                )
        else:
            if finished == LONG_BREAK:
                self.pomodoro_count = 1
                embed = discord.Embed(
                    title="🍅 Descanso terminado.",
                    description=f"Comenzando **Pomodoro 1/{self.durations.pomodoros_per_cycle}**.",
                    color=COLOR_WORK,
                )
            else:
                self.pomodoro_count += 1
                embed = discord.Embed(
                    title="🍅 Descanso terminado.",
                    description=f"Comenzando **Pomodoro {self.pomodoro_count}/{self.durations.pomodoros_per_cycle}**.",
                    color=COLOR_WORK,
                )
            self.phase = WORK
            self._remaining = float(self.durations.work)

        self._deadline = time.monotonic() + self._remaining
        self.paused = False

        await self.send(embed=embed)
        await self._refresh_main_message()
        await self._persist()

    # ---------------- voz / audio ----------------
    async def _ensure_voice(self) -> Optional[discord.VoiceClient]:
        vc = self.voice_client
        if vc is not None and vc.is_connected():
            return vc

        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return None

        # Si quedó un cliente huérfano en el guild, limpiarlo
        if guild.voice_client is not None and guild.voice_client is not vc:
            try:
                await guild.voice_client.disconnect(force=True)
            except Exception:
                pass

        try:
            self.voice_client = await self.voice_channel.connect(self_deaf=True)
            return self.voice_client
        except Exception:
            log.exception("No se pudo conectar/reconectar al canal de voz")
            return None

    async def _play_sound(self) -> None:
        if self.sound_path is None or not self.sound_path.is_file():
            log.warning("Sin archivo de sonido para guild %s", self.guild_id)
            return

        vc = await self._ensure_voice()
        if vc is None:
            await self.send(content="⚠️ No pude conectarme al canal de voz para reproducir el sonido.")
            return

        loop = asyncio.get_running_loop()
        done: asyncio.Future = loop.create_future()

        def _after(error: Optional[Exception]) -> None:
            if error:
                log.error("Error FFmpeg: %s", error)

            def _set() -> None:
                if not done.done():
                    done.set_result(None)

            loop.call_soon_threadsafe(_set)

        try:
            if vc.is_playing():
                vc.stop()
            source = discord.FFmpegPCMAudio(str(self.sound_path), before_options="-nostdin")
            vc.play(source, after=_after)
            await asyncio.wait_for(done, timeout=120)
        except asyncio.TimeoutError:
            log.error("Timeout esperando al audio")
        except Exception:
            log.exception("Error al reproducir audio")
            try:
                await self.send(content="⚠️ No se pudo reproducir el sonido de aviso.")
            except Exception:
                pass

    # ---------------- mensajes ----------------
    async def send(self, *, content: Optional[str] = None, embed: Optional[discord.Embed] = None,
                   view: Optional[discord.ui.View] = None) -> Optional[discord.Message]:
        if self.text_channel is None:
            return None
        try:
            return await self.text_channel.send(content=content, embed=embed, view=view)
        except Exception:
            log.exception("No se pudo enviar mensaje")
            return None

    # ---------------- embeds ----------------
    def main_embed(self) -> discord.Embed:
        if self.phase == WORK:
            color = COLOR_WORK
        elif self.phase == SHORT_BREAK:
            color = COLOR_SHORT
        else:
            color = COLOR_LONG

        state = "⏸️ EN PAUSA" if self.paused else PHASE_LABEL[self.phase]
        embed = discord.Embed(title="🍅 POMODORO", color=color)
        embed.add_field(name="Estado", value=state, inline=False)
        embed.add_field(name="Tiempo restante", value=f"**{format_time(self.remaining)}**", inline=False)
        embed.add_field(
            name="Pomodoro",
            value=f"{self.pomodoro_count}/{self.durations.pomodoros_per_cycle}",
            inline=False,
        )

        if self.phase == WORK:
            if self.pomodoro_count >= self.durations.pomodoros_per_cycle:
                nxt = f"Descanso largo — {format_time(self.durations.long_break)}"
            else:
                nxt = f"Descanso corto — {format_time(self.durations.short_break)}"
        elif self.phase == SHORT_BREAK:
            nxt = f"Pomodoro {self.pomodoro_count + 1}/{self.durations.pomodoros_per_cycle} — {format_time(self.durations.work)}"
        else:
            nxt = f"Pomodoro 1/{self.durations.pomodoros_per_cycle} — {format_time(self.durations.work)}"

        embed.add_field(name="Próximo", value=nxt, inline=False)
        embed.set_footer(text=f"Sesión de <@{self.owner_id}>")
        return embed

    def status_embed(self) -> discord.Embed:
        if self.stopped:
            return discord.Embed(title="🍅 Estado del Pomodoro",
                                 description="No hay ninguna sesión activa en este servidor.",
                                 color=COLOR_INFO)
        return self.main_embed()

    async def _refresh_main_message(self) -> None:
        if self._main_message is None:
            return
        try:
            await self._main_message.edit(embed=self.main_embed(), view=self._view)
        except discord.NotFound:
            self._main_message = None
        except Exception:
            log.exception("Error editando embed principal")

    # ---------------- acciones de botones ----------------
    async def skip_phase(self) -> bool:
        if self.stopped:
            return False
        self._remaining = 0.0
        self._deadline = time.monotonic()
        return True