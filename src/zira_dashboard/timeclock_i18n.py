"""Kiosk English→Spanish translation for personalized Timeclock screens.

Employees with an exact Odoo Spanish (Languages) level of 3 see Spanish
stacked above smaller English after they pick their name. Everyone else sees
English only. One glossary, one mode helper, one rendering helper. `t()` is
registered as a Jinja global in deps.py; templates call `{{ t("Clock Out") }}`
(optionally with format kwargs, e.g. `{{ t("Since {time}",
time=check_in_display) }}`).

An unknown English string always falls back to English (never blank), so a
missing glossary entry degrades gracefully.

Latin-American / Mexican shop-floor register. Edit a value here to fix any
wording — one line, one place.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from jinja2 import pass_context
from markupsafe import Markup, escape

from .shift_config import SITE_TZ

# English UI string -> Spanish. Keys must match the template literals exactly.
TRANSLATIONS: dict[str, str] = {
    # --- navigation ---
    "‹ Back": "‹ Atrás",
    "‹ Done": "‹ Listo",
    "Done": "Listo",
    # --- dashboard ---
    "You're clocked in": "Estás trabajando",
    "You're clocked in on": "Estás trabajando en",
    "Since {time}": "Desde {time}",
    "Clock Out": "Marcar salida",
    "Transfer": "Transferir",
    "Today you're scheduled on": "Hoy estás programado en",
    "Confirm — Clock In": "Confirmar — Marcar entrada",
    "I'm somewhere else": "Estoy en otro lugar",
    "You're not scheduled today": "No estás programado hoy",
    "Pick the work center you're on.": "Elige la estación donde estás trabajando.",
    "Pick Work Center": "Elegir estación",
    "On lunch — tap Sign Out only if you're leaving for the day.": "En el almuerzo — toque Salir solo si se va por el día.",
    "Time Off Request": "Solicitar tiempo libre",
    # --- pick work center ---
    "Pick where you're working": "Elige dónde estás trabajando",
    "Transfer to…": "Transferir a…",
    # --- punch success ---
    "Returning home…": "Regresando al inicio…",
    # --- sign-in notifications interstitial ---
    "Got it": "Entendido",
    "Time off approved": "Tiempo libre aprobado",
    "Time off denied": "Tiempo libre rechazado",
    "Time off cancelled": "Tiempo libre cancelado",
    "Your time off for {span} was approved. ✅": "Tu tiempo libre del {span} fue aprobado. ✅",
    "Your time off request for {span} was denied. ❌ See a supervisor if you have questions.": "Tu solicitud de tiempo libre del {span} fue rechazada. ❌ "
    "Habla con un supervisor si tienes preguntas.",
    "Your approved time off for {span} was cancelled. ⚠️ See a supervisor if you have questions.": "Tu tiempo libre aprobado del {span} fue cancelado. ⚠️ "
    "Habla con un supervisor si tienes preguntas.",
    "Saturday work cancelled": "Trabajo del sábado cancelado",
    "Saturday work was cancelled. Do not report to work.": "El trabajo del sábado fue cancelado. No te presentes a trabajar.",
    "Holiday work cancelled": "Trabajo del día festivo cancelado",
    "{name} work was cancelled. Do not report to work.": "El trabajo de {name} fue cancelado. No te presentes a trabajar.",
    # --- clock-out day-before reminder ---
    "Time off reminder": "Recordatorio de tiempo libre",
    "Heads up — you have approved time off {day}. Enjoy!": "Atención — tienes tiempo libre aprobado {day}. ¡Que lo disfrutes!",
    "Heads up — {day}, you're not due in until {ht} (approved).": "Atención — {day}, no entras hasta las {ht} (aprobado).",
    "Heads up — {day}, you have a late arrival (approved).": "Atención — {day}, tienes una llegada tarde (aprobado).",
    "Heads up — {day}, you can leave at {hf} (approved).": "Atención — {day}, puedes salir a las {hf} (aprobado).",
    "Heads up — {day}, you have an early leave (approved).": "Atención — {day}, tienes una salida temprana (aprobado).",
    "Heads up — {day}, you're off from {hf} to {ht} (approved).": "Atención — {day}, estás libre de {hf} a {ht} (aprobado).",
    "Heads up — {day}, you have partial time off (approved).": "Atención — {day}, tienes tiempo libre parcial (aprobado).",
    "Saturday work reminder": "Recordatorio de trabajo del sábado",
    "Holiday work reminder": "Recordatorio de trabajo en día festivo",
    "You are scheduled for {day}.": "Estás programado para {day}.",
    "Scheduled hours: {hours}": "Horario programado: {hours}",
    "Work area: {work_center}": "Área de trabajo: {work_center}",
    "Work area: check with your supervisor.": "Área de trabajo: consulta con tu supervisor.",
    # --- time off: landing ---
    "Time Off — {name}": "Tiempo libre — {name}",
    "Request Time Off": "Solicitar tiempo libre",
    "Full Day(s) Off": "Día(s) completo(s)",
    "Out for one or more whole days": "Ausente uno o más días completos",
    "Arriving Late": "Llegada tarde",
    "Tell us what time you'll arrive": "Dinos a qué hora llegarás",
    "Out for Part of the Day": "Ausente parte del día",
    "Leave + return on the same day": "Salir y regresar el mismo día",
    "Leaving Early": "Salida temprano",
    "Tell us what time you'll leave": "Dinos a qué hora te irás",
    "My Requests": "Mis solicitudes",
    "Who's Out": "Quién está ausente",
    # --- time off: request details ---
    "Mid-Day Gap": "Ausencia a media jornada",
    "Editing existing request": "Editando solicitud existente",
    "Type": "Tipo",
    "Type:": "Tipo:",
    "· Unpaid": "· Sin goce",
    "Available": "Disponible",
    "This request": "Esta solicitud",
    "Remaining after": "Restante después",
    "Start date": "Fecha de inicio",
    "End date": "Fecha de fin",
    "Date": "Fecha",
    "I'll arrive at": "Llegaré a las",
    "I'll leave at": "Saldré a las",
    "Gone from": "Ausente desde",
    "To": "Hasta",
    "Note (optional)": "Nota (opcional)",
    "Save Changes": "Guardar cambios",
    "Submit Request": "Enviar solicitud",
    # --- time off: calendar (Who's Out) ---
    "Who's Out — {heading}": "Quién está ausente — {heading}",
    "Mon": "Lun",
    "Tue": "Mar",
    "Wed": "Mié",
    "Thu": "Jue",
    "Fri": "Vie",
    "Sat": "Sáb",
    "Sun": "Dom",
    "more": "más",
    "Show less": "Mostrar menos",
    "Prev": "Anterior",
    "Next": "Siguiente",
    "Today": "Hoy",
    # --- time off: my requests + detail ---
    "My Requests — {name}": "Mis solicitudes — {name}",
    "Time Off": "Tiempo libre",
    "No requests yet.": "Aún no hay solicitudes.",
    "Request Details": "Detalles de la solicitud",
    "Dates": "Fechas",
    "Hours": "Horas",
    "Shape": "Modalidad",
    "Note": "Nota",
    "Sync error": "Error de sincronización",
    "Edit Request": "Editar solicitud",
    "Canceling an approved request will need approval again if you change your mind.": "Cancelar una solicitud aprobada requerirá aprobación nuevamente si cambias de opinión.",
    "Cancel This Request": "Cancelar esta solicitud",
    # --- time off: overlap conflict modal ---
    "You already have time off for this time so we can't add a second. Either cancel your request via the My Requests button or contact management for help.": "Ya tienes tiempo libre para estas fechas, así que no podemos agregar otro. Cancela tu solicitud con el botón Mis solicitudes o comunícate con la gerencia para obtener ayuda.",
    "Go to My Requests": "Ir a Mis solicitudes",
    "OK": "Aceptar",
    # --- time off: salaried landing ---
    "You're salaried, so there's nothing to clock — just request time off below.": "Eres asalariado, así que no necesitas marcar entrada ni salida — solo solicita tu tiempo libre aquí abajo.",
    # --- time off: submitted ---
    "Request Submitted": "Solicitud enviada",
    "Your time-off request from {start} to {end} is pending approval.": "Tu solicitud de tiempo libre del {start} al {end} está pendiente de aprobación.",
    # --- optional Saturday work ---
    "Saturday Work Available": "Trabajo disponible el sábado",
    "Holiday Work Available — {name}": "Trabajo disponible en el día festivo — {name}",
    "Can you work Saturday, {date}?": "¿Puedes trabajar el sábado {date}?",
    "Can you work this holiday, {name}, on {date}?": "¿Puedes trabajar este día festivo, {name}, el {date}?",
    "Respond by {deadline}.": "Responde antes de {deadline}.",
    "Openings may fill before the deadline.": "Los lugares pueden llenarse antes de la fecha límite.",
    "Yes": "Sí",
    "No": "No",
    "Decide later": "Decidir después",
    "I can work only part of the shift": "Solo puedo trabajar parte del turno",
    "I can work only part of the shift — {name}": "Solo puedo trabajar parte del turno — {name}",
    "Confirm your commitment": "Confirma tu compromiso",
    "By confirming, you commit to work Saturday from {hours}.": "Al confirmar, te comprometes a trabajar el sábado de {hours}.",
    "By confirming, you commit to work this holiday from {hours}.": "Al confirmar, te comprometes a trabajar este día festivo de {hours}.",
    "You may cancel until {deadline}.": "Puedes cancelar hasta {deadline}.",
    "After that, contact a manager.": "Después de esa hora, habla con un gerente.",
    "Your Saturday commitment": "Tu compromiso del sábado",
    "Your holiday work commitment — {name}": "Tu compromiso de trabajo en día festivo — {name}",
    "Cancel Saturday commitment": "Cancelar compromiso del sábado",
    "Cancel holiday work commitment": "Cancelar compromiso de trabajo en día festivo",
    "Contact a manager to make a change.": "Habla con un gerente para hacer un cambio.",
    "This is a firm commitment.": "Este es un compromiso firme.",
    "Start": "Inicio",
    "End": "Fin",
    "Availability must use 30-minute increments and stay within the Saturday shift.": "La disponibilidad debe usar incrementos de 30 minutos y mantenerse dentro del turno del sábado.",
    "Availability must use 30-minute increments and stay within the optional shift.": "La disponibilidad debe usar incrementos de 30 minutos y mantenerse dentro del turno opcional.",
    "That opening was just filled. You have not been scheduled.": "Ese lugar se acaba de llenar. No has sido programado.",
}

LanguageMode = Literal["en", "es_primary"]


def language_mode_for_person(person: dict | None) -> LanguageMode:
    """Return the personalized Timeclock language mode for one employee."""
    level = person.get("spanish_level") if person else None
    if type(level) is int and level == 3:
        return "es_primary"
    return "en"


def context_for_person(person: dict | None) -> dict[str, LanguageMode]:
    """Return the template context needed for personalized language output."""
    return {"timeclock_language": language_mode_for_person(person)}


def _fill(template: str, kwargs: dict) -> Markup:
    """Escape the template text and any substituted values, then format."""
    safe = escape(template)
    if not kwargs:
        return safe
    return safe.format(**{k: escape(v) for k, v in kwargs.items()})


_SPANISH_WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_SPANISH_MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


def spanish_date_label(value: date | datetime) -> str:
    return (
        f"{_SPANISH_WEEKDAYS[value.weekday()]}, {value.day} de {_SPANISH_MONTHS[value.month - 1]}"
    )


def spanish_deadline_label(value: datetime) -> str:
    """Format a database deadline in the plant's local timezone.

    PostgreSQL TIMESTAMPTZ values arrive timezone-aware (normally UTC), while
    employee-facing Saturday deadlines are always communicated in plant time.
    """
    value = value.astimezone(SITE_TZ)
    clock = value.strftime("%I:%M %p").lstrip("0")
    return f"{spanish_date_label(value)} a las {clock}"


@pass_context
def td(ctx, text: str, *, es: dict | None = None, en: dict | None = None) -> str | Markup:
    """Translate text whose interpolated date/value also differs by language."""
    english = _fill(text, en or {})
    if ctx.get("timeclock_language", "en") != "es_primary":
        return english
    spanish = _fill(TRANSLATIONS.get(text, text), es if es is not None else (en or {}))
    return Markup(
        '<span class="k-bi k-bi-es-primary"><span class="k-es k-primary">{}</span>'
        '<span class="k-en k-secondary">{}</span></span>'
    ).format(spanish, english)


@pass_context
def t(ctx, text: str, **kwargs) -> str | Markup:
    """Translate a UI string for the current render.

    Spanish-primary output is available only for the explicit
    ``timeclock_language`` context value. Unknown strings fall back to English.
    """
    english = _fill(text, kwargs)
    if ctx.get("timeclock_language", "en") != "es_primary":
        return english
    spanish_tmpl = TRANSLATIONS.get(text)
    if not spanish_tmpl:
        return english  # graceful fallback — never blank
    spanish = _fill(spanish_tmpl, kwargs)
    return Markup(
        '<span class="k-bi k-bi-es-primary">'
        '<span class="k-es k-primary">{}</span>'
        '<span class="k-en k-secondary">{}</span>'
        "</span>"
    ).format(spanish, english)
