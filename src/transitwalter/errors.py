"""Feste Fehlercodes – für Automatisierungen robuster als die lesbaren Meldungen.

Tool-Fehler haben die Form "CODE: Meldung", zusätzlich strukturiert als {"error": {"code", "message"}}.
"""

from mcp.server.mcpserver.exceptions import ToolError

INVALID_ARGUMENT = "INVALID_ARGUMENT"      # ungültiger Parameter (Zeit, Koordinaten, Anzahl …)
PLACE_NOT_FOUND = "PLACE_NOT_FOUND"        # Ort/Haltestelle nicht gefunden
AIRPORT_NOT_FOUND = "AIRPORT_NOT_FOUND"    # IATA-Code unbekannt
EXPORT_NOT_FOUND = "EXPORT_NOT_FOUND"      # itinerary_id unbekannt oder abgelaufen
RATE_LIMITED = "RATE_LIMITED"              # eigenes Limit oder Transitous drosselt
CONFIG_ERROR = "CONFIG_ERROR"              # z. B. TRANSITOUS_USER_AGENT fehlt
UPSTREAM_ERROR = "UPSTREAM_ERROR"          # sonstiger Fehler bei/von Transitous

ALL = [INVALID_ARGUMENT, PLACE_NOT_FOUND, AIRPORT_NOT_FOUND, EXPORT_NOT_FOUND, RATE_LIMITED, CONFIG_ERROR, UPSTREAM_ERROR]


class TransitError(ToolError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")
