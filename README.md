# 🍅 Pomodoro Bot para Discord

Bot Pomodoro con SQLite, estadísticas, sonido personalizado, botones y modo prueba.

## Ciclo
25/5 → 25/5 → 25/5 → 25/15 → repite.

## Comandos
- `/pomodoro iniciar`
- `/pomodoro pausa`
- `/pomodoro continuar`
- `/pomodoro detener`
- `/pomodoro estado`
- `/pomodoro estadisticas`
- `/pomodoro sonido subir <archivo>`
- `/pomodoro sonido actual`
- `/pomodoro sonido probar`
- `/pomodoro sonido eliminar`
- `/pomodoro ayuda`

## Modo prueba
En `.env`: `POMODORO_TEST=1`. Duraciones: 10s / 5s / 8s.

## Instalación (Pop!_OS)
Ver sección "Tutorial paso a paso" del mensaje del asistente.

## Persistencia
- Configuración del sonido
- Sesiones activas (recuperables tras reinicio)
- Pomodoros completados
- Historial de sesiones

Todo vive en `data/pomodoro.db`.

## Permisos necesarios
Ver canales, enviar mensajes, insertar embeds, conectar, hablar, usar slash commands.