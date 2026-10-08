#!/usr/bin/env python3
"""Bot de Discord Pomodoro — versión con SQLite, estadísticas y botones."""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

from config import (
    ALLOWED_AUDIO_EXT, AUDIO_DIR, DB_PATH, MAX_SOUND_BYTES, TEST_MODE,
)
from database import Database, parse_iso
from pomodoro import (
    COLOR_ERROR, COLOR_INFO, COLOR_WORK,
    LONG_BREAK, SHORT_BREAK, WORK,
    Durations, PomodoroSession, default_durations, format_time,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_RAW = os.getenv("GUILD_ID")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pomodoro-bot")


# --------------------------------------------------------------------------- #
# Utilidades de sonido
# --------------------------------------------------------------------------- #
def guild_sound_path(guild_id: int) -> Optional[Path]:
    for f in AUDIO_DIR.glob(f"guild_{guild_id}.*"):
        if f.is_file():
            return f
    return None


def default_sound_path() -> Optional[Path]:
    for ext in ALLOWED_AUDIO_EXT:
        p = AUDIO_DIR / f"default{ext}"
        if p.is_file():
            return p
    return None


def resolve_sound(guild_id: int) -> Optional[Path]:
    return guild_sound_path(guild_id) or default_sound_path()


# --------------------------------------------------------------------------- #
# Vista con botones
# --------------------------------------------------------------------------- #
class ControlView(discord.ui.View):
    def __init__(self, session: PomodoroSession) -> None:
        super().__init__(timeout=None)
        self.session = session

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        sess = client.sessions.get(self.session.guild_id)
        if sess is None or sess is not self.session or self.session.stopped:
            await interaction.response.send_message(
                "⚠️ Esta sesión Pomodoro ya no está activa.", ephemeral=True)
            return False

        if interaction.user.id == self.session.owner_id:
            return True

        if isinstance(interaction.user, discord.Member):
            ch = interaction.channel
            if ch is not None:
                perms = ch.permissions_for(interaction.user)
                if perms.manage_guild or perms.manage_channels:
                    return True

        await interaction.response.send_message(
            "🔒 Solo quien inició la sesión (o un moderador) puede controlarla.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Pausar", emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def btn_pause(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self._authorize(interaction):
            return
        if self.session.pause():
            await interaction.response.send_message("⏸️ Pausado.", ephemeral=True)
            await self.session._refresh_main_message()
        else:
            await interaction.response.send_message("⚠️ Ya está pausado.", ephemeral=True)

    @discord.ui.button(label="Continuar", emoji="▶️", style=discord.ButtonStyle.success)
    async def btn_resume(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self._authorize(interaction):
            return
        if self.session.resume():
            await interaction.response.send_message("▶️ Reanudado.", ephemeral=True)
            await self.session._refresh_main_message()
        else:
            await interaction.response.send_message("⚠️ No está pausado.", ephemeral=True)

    @discord.ui.button(label="Saltar", emoji="⏭️", style=discord.ButtonStyle.primary)
    async def btn_skip(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self._authorize(interaction):
            return
        await interaction.response.send_message("⏭️ Período saltado.", ephemeral=True)
        await self.session.skip_phase()

    @discord.ui.button(label="Detener", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def btn_stop(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not await self._authorize(interaction):
            return
        await interaction.response.send_message("⏹️ Deteniendo sesión…", ephemeral=True)
        await _stop_session(self.session.guild_id, notify_channel=True)


# --------------------------------------------------------------------------- #
# Cliente
# --------------------------------------------------------------------------- #
class PomodoroBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.sessions: Dict[int, PomodoroSession] = {}
        self.db = Database(DB_PATH)

    async def setup_hook(self) -> None:
        await self.db.connect()

        pomodoro_group = build_pomodoro_group()
        self.tree.add_command(pomodoro_group)

        if GUILD_ID_RAW:
            try:
                guild_obj = discord.Object(id=int(GUILD_ID_RAW))
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                log.info("Comandos sincronizados en guild %s (%d).", GUILD_ID_RAW, len(synced))
            except Exception:
                log.exception("Error sincronizando comandos en el guild indicado")
        else:
            synced = await self.tree.sync()
            log.info("Comandos globales sincronizados (%d).", len(synced))

        # Recovery de sesiones
        await self._recover_sessions()


client = PomodoroBot()


async def _recover_sessions() -> None:
    rows = await client.db.load_active_sessions()
    if not rows:
        return

    log.info("Recuperando %d sesiones activas…", len(rows))
    for row in rows:
        try:
            guild = client.get_guild(int(row["guild_id"]))
            if guild is None:
                await client.db.delete_active_session(int(row["guild_id"]))
                continue

            voice_ch = guild.get_channel(int(row["voice_channel_id"]))
            text_ch = guild.get_channel(int(row["text_channel_id"]))
            if voice_ch is None or text_ch is None:
                await client.db.delete_active_session(int(row["guild_id"]))
                continue

            sound = resolve_sound(guild.id)
            sess = PomodoroSession(
                bot=client, db=client.db, guild=guild,
                owner_id=int(row["owner_id"]),
                voice_client=guild.voice_client,
                voice_channel=voice_ch,
                text_channel=text_ch,
                sound_path=sound,
                phase=row["phase"],
                pomodoro_count=int(row["pomodoro_count"]),
                total_work_done=int(row["total_work_done"]),
                session_started_at=parse_iso(row["session_started_at"]),
                remaining=float(row["remaining_seconds"]),
                paused=bool(row["paused"]),
            )
            view = ControlView(sess)
            sess._view = view
            msg = await text_ch.send(
                content="🔄 Sesión recuperada tras reinicio del bot.",
                embed=sess.main_embed(),
                view=view,
            )
            sess._main_message = msg
            client.sessions[guild.id] = sess
            sess.start()
            log.info("Sesión recuperada en guild %s", guild.id)
        except Exception:
            log.exception("Error recuperando sesión")


async def _stop_session(guild_id: int, *, notify_channel: bool) -> bool:
    sess = client.sessions.pop(guild_id, None)
    if sess is None:
        return False
    if notify_channel:
        await sess.send(content="⏹️ Sesión Pomodoro detenida.")
    await sess.stop(disconnect=True, record=True)
    return True


def _check_voice_perms(channel: discord.VoiceChannel, me: discord.Member) -> Optional[str]:
    perms = channel.permissions_for(me)
    if not perms.view_channel:
        return "No tengo permiso para **ver** ese canal de voz."
    if not perms.connect:
        return "No tengo permiso para **conectarme** a ese canal de voz."
    if not perms.speak:
        return "No tengo permiso para **hablar** en ese canal de voz."
    return None


# --------------------------------------------------------------------------- #
# Grupo de comandos
# --------------------------------------------------------------------------- #
def build_pomodoro_group() -> app_commands.Group:
    group = app_commands.Group(name="pomodoro", description="Temporizador Pomodoro 🍅")

    # ---- INICIAR ---- #
    @group.command(name="iniciar", description="Inicia una sesión Pomodoro y entra a tu canal de voz")
    async def iniciar(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Solo en servidores.", ephemeral=True)
            return

        if guild.id in client.sessions:
            await interaction.response.send_message(
                "⚠️ Ya hay una sesión Pomodoro activa en este servidor. "
                "Usa `/pomodoro detener` primero.",
                ephemeral=True,
            )
            return

        member = interaction.user
        vs = getattr(member, "voice", None)
        if vs is None or vs.channel is None:
            await interaction.response.send_message(
                "🔇 Debes estar en un canal de voz para iniciar el Pomodoro.",
                ephemeral=True,
            )
            return

        channel = vs.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message(
                "❌ Debes estar en un canal de voz normal (no de escenario).",
                ephemeral=True,
            )
            return

        err = _check_voice_perms(channel, guild.me)
        if err:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return

        await interaction.response.defer()

        try:
            existing = guild.voice_client
            if existing is not None and existing.is_connected():
                if existing.channel.id != channel.id:
                    await existing.move_to(channel)
                vc = existing
            else:
                vc = await channel.connect(self_deaf=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ Sin permisos para entrar al canal de voz.")
            return
        except Exception as exc:
            log.exception("Error conectando a voz")
            await interaction.followup.send(f"❌ No pude conectarme al canal de voz: `{exc}`")
            return

        sound = resolve_sound(guild.id)
        sess = PomodoroSession(
            bot=client, db=client.db, guild=guild,
            owner_id=member.id, voice_client=vc,
            voice_channel=channel, text_channel=interaction.channel,
            sound_path=sound,
        )
        view = ControlView(sess)
        sess._view = view
        msg = await interaction.followup.send(embed=sess.main_embed(), view=view, wait=True)
        sess._main_message = msg

        client.sessions[guild.id] = sess
        sess.start()
        await sess._persist()

        if sound is None:
            await interaction.followup.send(
                "⚠️ No hay sonido configurado. Súbelo con `/pomodoro sonido subir` "
                "o coloca `audio/default.mp3`.",
            )

    # ---- PAUSA ---- #
    @group.command(name="pausa", description="Pausa el temporizador Pomodoro")
    async def pausa(interaction: discord.Interaction) -> None:
        sess = client.sessions.get(interaction.guild_id) if interaction.guild else None
        if sess is None or sess.stopped:
            await interaction.response.send_message("⚠️ No hay sesión activa.", ephemeral=True)
            return
        if not _can_control(interaction, sess):
            await interaction.response.send_message("🔒 No tienes permiso para controlar esta sesión.", ephemeral=True)
            return
        if sess.pause():
            await interaction.response.send_message(
                f"⏸️ Pausado. Restante: **{format_time(sess.remaining)}**")
            await sess._refresh_main_message()
        else:
            await interaction.response.send_message("⚠️ Ya está pausado.", ephemeral=True)

    # ---- CONTINUAR ---- #
    @group.command(name="continuar", description="Reanuda el temporizador Pomodoro")
    async def continuar(interaction: discord.Interaction) -> None:
        sess = client.sessions.get(interaction.guild_id) if interaction.guild else None
        if sess is None or sess.stopped:
            await interaction.response.send_message("⚠️ No hay sesión activa.", ephemeral=True)
            return
        if not _can_control(interaction, sess):
            await interaction.response.send_message("🔒 No tienes permiso para controlar esta sesión.", ephemeral=True)
            return
        if sess.resume():
            await interaction.response.send_message(
                f"▶️ Reanudado. Restante: **{format_time(sess.remaining)}**")
            await sess._refresh_main_message()
        else:
            await interaction.response.send_message("⚠️ No está pausado.", ephemeral=True)

    # ---- DETENER ---- #
    @group.command(name="detener", description="Detiene la sesión Pomodoro")
    async def detener(interaction: discord.Interaction) -> None:
        sess = client.sessions.get(interaction.guild_id) if interaction.guild else None
        if sess is None or sess.stopped:
            await interaction.response.send_message("⚠️ No hay sesión activa.", ephemeral=True)
            return
        if not _can_control(interaction, sess):
            await interaction.response.send_message("🔒 No tienes permiso para controlar esta sesión.", ephemeral=True)
            return
        await interaction.response.send_message("⏹️ Deteniendo sesión…")
        await _stop_session(sess.guild_id, notify_channel=False)

    # ---- ESTADO ---- #
    @group.command(name="estado", description="Muestra el estado actual del Pomodoro")
    async def estado(interaction: discord.Interaction) -> None:
        sess = client.sessions.get(interaction.guild_id) if interaction.guild else None
        if sess is None or sess.stopped:
            embed = discord.Embed(
                title="🍅 Estado del Pomodoro",
                description="No hay sesión activa. Usa `/pomodoro iniciar`.",
                color=COLOR_INFO,
            )
        else:
            embed = sess.status_embed()
        await interaction.response.send_message(embed=embed)

    # ---- ESTADÍSTICAS ---- #
    @group.command(name="estadisticas", description="Muestra tus estadísticas de estudio")
    async def estadisticas(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            stats = await client.db.get_stats(interaction.user.id)
        except Exception:
            log.exception("Error obteniendo estadísticas")
            await interaction.followup.send("❌ No pude obtener las estadísticas.", ephemeral=True)
            return

        embed = discord.Embed(title="📊 Estadísticas", color=COLOR_INFO)
        embed.add_field(
            name="Hoy",
            value=(
                f"Pomodoros: **{stats['today_count']}**\n"
                f"Tiempo: **{_fmt_dur(stats['today_seconds'])}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Esta semana",
            value=(
                f"Pomodoros: **{stats['week_count']}**\n"
                f"Tiempo: **{_fmt_dur(stats['week_seconds'])}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Total",
            value=(
                f"Pomodoros: **{stats['total_count']}**\n"
                f"Tiempo: **{_fmt_dur(stats['total_seconds'])}**"
            ),
            inline=True,
        )
        embed.add_field(name="Sesiones completadas", value=f"**{stats['sessions']}**", inline=True)
        embed.add_field(name="Racha de días", value=f"**{stats['streak']}** 🔥", inline=True)
        embed.add_field(
            name="Promedio diario",
            value=f"**{_fmt_dur(int(stats['avg_daily_seconds']))}**",
            inline=True,
        )
        await interaction.followup.send(embed=embed)

    # ---- SONIDO (grupo anidado) ---- #
    sonido = app_commands.Group(name="sonido", description="Configura el sonido de aviso")
    group.add_command(sonido)

    @sonido.command(name="subir", description="Sube un MP3/WAV/OGG como sonido de aviso")
    @app_commands.describe(archivo="Archivo de audio (MP3, WAV, OGG, M4A, FLAC…)")
    async def sonido_subir(interaction: discord.Interaction, archivo: discord.Attachment) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Solo en servidores.", ephemeral=True)
            return

        ext = Path(archivo.filename or "").suffix.lower()
        if ext not in ALLOWED_AUDIO_EXT:
            await interaction.response.send_message(
                f"❌ Formato `{ext or 'desconocido'}` no soportado.\n"
                f"Permitidos: {', '.join(sorted(ALLOWED_AUDIO_EXT))}",
                ephemeral=True,
            )
            return

        if archivo.size > MAX_SOUND_BYTES:
            await interaction.response.send_message(
                f"❌ El archivo pesa {archivo.size / 1024 / 1024:.2f} MB. "
                f"Máx: {MAX_SOUND_BYTES // 1024 // 1024} MB.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        for old in AUDIO_DIR.glob(f"guild_{guild.id}.*"):
            try:
                old.unlink()
            except OSError:
                pass

        dest = AUDIO_DIR / f"guild_{guild.id}{ext}"
        try:
            await archivo.save(dest)
        except Exception as exc:
            log.exception("Error guardando sonido")
            await interaction.followup.send(f"❌ No se pudo guardar: `{exc}`", ephemeral=True)
            return

        await client.db.set_guild_sound(guild.id, dest.name)

        sess = client.sessions.get(guild.id)
        if sess is not None and not sess.stopped:
            sess.sound_path = dest

        await interaction.followup.send(
            f"✅ Sonido guardado como `{dest.name}`. Se usará en los próximos avisos.",
            ephemeral=True,
        )

    @sonido.command(name="actual", description="Muestra el sonido configurado")
    async def sonido_actual(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Solo en servidores.", ephemeral=True)
            return

        custom = guild_sound_path(guild.id)
        default = default_sound_path()
        embed = discord.Embed(title="🔊 Sonido configurado", color=COLOR_INFO)
        embed.add_field(
            name="Personalizado del servidor",
            value=f"`{custom.name}`" if custom else "❌ No configurado",
            inline=False,
        )
        embed.add_field(
            name="Por defecto (fallback)",
            value=f"`{default.name}`" if default else "❌ No existe `audio/default.*`",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @sonido.command(name="probar", description="Reproduce el sonido en tu canal de voz")
    async def sonido_probar(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("❌ Solo en servidores.", ephemeral=True)
            return

        vs = getattr(member, "voice", None)
        if vs is None or vs.channel is None:
            await interaction.response.send_message(
                "🔇 Debes estar en un canal de voz para probar el sonido.", ephemeral=True)
            return

        sound = resolve_sound(guild.id)
        if sound is None:
            await interaction.response.send_message(
                "❌ No hay ningún sonido configurado. Usa `/pomodoro sonido subir`.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            vc = guild.voice_client
            if vc is None or not vc.is_connected():
                vc = await vs.channel.connect(self_deaf=True)
            elif vc.channel.id != vs.channel.id:
                await vc.move_to(vs.channel)
        except Exception as exc:
            log.exception("Error conectando para probar sonido")
            await interaction.followup.send(f"❌ No pude conectar al canal: `{exc}`", ephemeral=True)
            return

        done = interaction.client.loop.create_future()

        def _after(err):
            if err:
                log.error("Error reproduciendo prueba: %s", err)
            interaction.client.loop.call_soon_threadsafe(
                lambda: done.done() or done.set_result(None)
            )

        try:
            if vc.is_playing():
                vc.stop()
            vc.play(discord.FFmpegPCMAudio(str(sound), before_options="-nostdin"), after=_after)
            await asyncio.wait_for(done, timeout=60)
            await interaction.followup.send(f"✅ Reproducido `{sound.name}`.", ephemeral=True)
        except Exception as exc:
            log.exception("Error al probar sonido")
            await interaction.followup.send(f"❌ Error: `{exc}`", ephemeral=True)

    @sonido.command(name="eliminar", description="Elimina el sonido personalizado del servidor")
    async def sonido_eliminar(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Solo en servidores.", ephemeral=True)
            return

        removed = False
        for f in AUDIO_DIR.glob(f"guild_{guild.id}.*"):
            try:
                f.unlink()
                removed = True
            except OSError:
                pass

        await client.db.set_guild_sound(guild.id, None)

        sess = client.sessions.get(guild.id)
        if sess is not None and not sess.stopped:
            sess.sound_path = default_sound_path()

        await interaction.response.send_message(
            "🗑️ Sonido eliminado." if removed else "ℹ️ No había sonido personalizado.",
            ephemeral=True,
        )

    # ---- AYUDA ---- #
    @group.command(name="ayuda", description="Muestra la ayuda del bot Pomodoro")
    async def ayuda(interaction: discord.Interaction) -> None:
        mode = "🧪 **MODO PRUEBA** (duraciones cortas)" if TEST_MODE else "⏱️ Modo normal (25/5/15)"
        embed = discord.Embed(
            title="🍅 Ayuda — Pomodoro",
            description=f"Ciclo: **25/5 → 25/5 → 25/5 → 25/15**.\n{mode}",
            color=COLOR_INFO,
        )
        embed.add_field(name="/pomodoro iniciar", value="Inicia sesión y entra a tu canal de voz.", inline=False)
        embed.add_field(name="/pomodoro pausa", value="Pausa el temporizador.", inline=False)
        embed.add_field(name="/pomodoro continuar", value="Reanuda el temporizador.", inline=False)
        embed.add_field(name="/pomodoro detener", value="Detiene la sesión.", inline=False)
        embed.add_field(name="/pomodoro estado", value="Muestra el estado actual.", inline=False)
        embed.add_field(name="/pomodoro estadisticas", value="Muestra tus estadísticas.", inline=False)
        embed.add_field(name="/pomodoro sonido subir <archivo>", value="Sube un sonido personalizado.", inline=False)
        embed.add_field(name="/pomodoro sonido actual", value="Muestra el sonido configurado.", inline=False)
        embed.add_field(name="/pomodoro sonido probar", value="Reproduce el sonido en tu canal.", inline=False)
        embed.add_field(name="/pomodoro sonido eliminar", value="Elimina el sonido personalizado.", inline=False)
        embed.set_footer(text="Botones: ⏸️ ▶️ ⏭️ ⏹️ (solo el dueño o moderadores)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    return group


def _can_control(interaction: discord.Interaction, sess: PomodoroSession) -> bool:
    if interaction.user.id == sess.owner_id:
        return True
    if isinstance(interaction.user, discord.Member):
        ch = interaction.channel
        if ch is not None:
            p = ch.permissions_for(interaction.user)
            return p.manage_guild or p.manage_channels
    return False


def _fmt_dur(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    m = r // 60
    if h:
        return f"{h}h {m}min"
    return f"{m}min"


# --------------------------------------------------------------------------- #
# Errores
# --------------------------------------------------------------------------- #
@client.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    log.exception("Error en comando", exc_info=error)
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ Espera {error.retry_after:.1f}s."
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "❌ No tienes permisos."
    else:
        msg = f"❌ Error: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Eventos
# --------------------------------------------------------------------------- #
@client.event
async def on_ready() -> None:
    log.info("Conectado como %s (ID %s)", client.user, client.user.id)
    await client.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="🍅 /pomodoro iniciar")
    )


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if client.user is None or member.id != client.user.id:
        return
    if before.channel is None or after.channel is not None:
        return

    sess = client.sessions.get(member.guild.id)
    if sess is None or sess.stopped:
        return

    async def _reconnect() -> None:
        for attempt in range(1, 4):
            await asyncio.sleep(2 * attempt)
            if sess.stopped:
                return
            vc = await sess._ensure_voice()
            if vc is not None:
                await sess.send(content="🔄 Reconectado al canal de voz.")
                return
        await sess.send(content="❌ No pude reconectar al canal de voz. Deteniendo la sesión.")
        await _stop_session(sess.guild_id, notify_channel=False)

    asyncio.create_task(_reconnect())


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    if not TOKEN:
        log.error("Falta DISCORD_TOKEN. Copia .env.example a .env y pega tu token.")
        sys.exit(1)
    try:
        client.run(TOKEN)
    except discord.LoginFailure:
        log.error("Token inválido.")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Detenido por el usuario.")


if __name__ == "__main__":
    main()