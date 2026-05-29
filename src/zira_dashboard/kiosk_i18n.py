"""Kiosk English→Spanish translation for bilingual employees.

Employees with an Odoo Spanish (Languages) skill see Spanish stacked under
English on every screen after they pick their name. One glossary, one
helper. `t()` is registered as a Jinja global in deps.py; templates call
`{{ t("Clock Out") }}` (optionally with format kwargs, e.g.
`{{ t("Since {time}", time=check_in_display) }}`).

Register format: when the render context flag `bilingual` is False (the
default), `t()` returns plain English; when True, it returns stacked
`<span class="k-en">…</span><span class="k-es">…</span>` markup. An
unknown English string falls back to English (never blank), so a missing
glossary entry degrades gracefully.

Latin-American / Mexican shop-floor register. Edit a value here to fix any
wording — one line, one place.
"""
from __future__ import annotations

from jinja2 import pass_context
from markupsafe import Markup, escape

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
    "Time Off Request": "Solicitar tiempo libre",
    # --- pick work center ---
    "Pick where you're working": "Elige dónde estás trabajando",
    "Transfer to…": "Transferir a…",
    # --- punch success ---
    "Returning home…": "Regresando al inicio…",
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
    "Canceling an approved request will need approval again if you change your mind.":
        "Cancelar una solicitud aprobada requerirá aprobación nuevamente si cambias de opinión.",
    "Cancel This Request": "Cancelar esta solicitud",
    # --- time off: submitted ---
    "Request Submitted": "Solicitud enviada",
    "Your time-off request from {start} to {end} is pending approval.":
        "Tu solicitud de tiempo libre del {start} al {end} está pendiente de aprobación.",
}


def _fill(template: str, kwargs: dict) -> Markup:
    """Escape the template text and any substituted values, then format."""
    safe = escape(template)
    if not kwargs:
        return safe
    return safe.format(**{k: escape(v) for k, v in kwargs.items()})


@pass_context
def t(ctx, text: str, **kwargs) -> str | Markup:
    """Translate a UI string for the current render. English-only unless the
    context flag `bilingual` is True; then English + Spanish stacked. Unknown
    strings fall back to English."""
    english = _fill(text, kwargs)
    if not ctx.get("bilingual"):
        return english
    spanish_tmpl = TRANSLATIONS.get(text)
    if not spanish_tmpl:
        return english  # graceful fallback — never blank
    spanish = _fill(spanish_tmpl, kwargs)
    return Markup('<span class="k-en">{}</span><span class="k-es">{}</span>').format(
        english, spanish
    )
