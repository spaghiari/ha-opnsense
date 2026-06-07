"""Client API OPNsense pour Home Assistant."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from aiohttp import BasicAuth, ClientError, ClientTimeout

from .const import API_ENDPOINTS, HTTP_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class OPNsenseApiError(Exception):
    """Erreur générique de l'API OPNsense."""


class OPNsenseAuthError(OPNsenseApiError):
    """Erreur d'authentification (401)."""


class OPNsenseForbiddenError(OPNsenseApiError):
    """Erreur d'autorisation - privilège manquant (403)."""


class OPNsenseApiClient:
    """Client asynchrone vers l'API REST d'OPNsense.

    Gère l'authentification Basic, le verify SSL configurable,
    et fournit des méthodes typées pour chaque endpoint utilisé.
    """

    def __init__(
        self,
        host: str,
        port: int,
        api_key: str,
        api_secret: str,
        session: aiohttp.ClientSession,
        verify_ssl: bool = False,
    ) -> None:
        """Initialise le client.

        host : adresse IP ou hostname (ex: '192.168.1.1')
        port : port HTTPS (ex: 443)
        api_key / api_secret : credentials générés depuis OPNsense
        session : session aiohttp partagée (fournie par HA)
        verify_ssl : True pour vérifier le cert TLS (False par défaut, cert self-signed)
        """
        self._base_url = f"https://{host}:{port}"
        self._auth = BasicAuth(api_key, api_secret)
        self._session = session
        self._verify_ssl = verify_ssl
        self._timeout = ClientTimeout(total=HTTP_TIMEOUT)

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Effectue une requête HTTP brute vers l'API.

        Gère les codes d'erreur courants et lève une exception typée.
        """
        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(
                method,
                url,
                json=payload,
                auth=self._auth,
                ssl=self._verify_ssl,
                timeout=self._timeout,
            ) as response:
                if response.status == 401:
                    raise OPNsenseAuthError(
                        "Authentification refusée - clé API invalide"
                    )
                if response.status == 403:
                    raise OPNsenseForbiddenError(
                        f"Accès refusé sur {path} - privilège manquant côté OPNsense"
                    )
                if response.status >= 400:
                    text = await response.text()
                    raise OPNsenseApiError(
                        f"Erreur HTTP {response.status} sur {path}: {text[:200]}"
                    )
                # Certains endpoints renvoient une chaîne, d'autres du JSON
                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type:
                    return await response.json()
                text = await response.text()
                return {"raw": text}
        except TimeoutError as err:
            raise OPNsenseApiError(f"Timeout sur {path}") from err
        except ClientError as err:
            raise OPNsenseApiError(f"Erreur réseau sur {path}: {err}") from err

    async def get(self, endpoint_key: str) -> dict[str, Any]:
        """Appel GET sur un endpoint référencé dans API_ENDPOINTS."""
        path = API_ENDPOINTS[endpoint_key]
        return await self._request("GET", path)

    async def post(
        self, endpoint_key: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Appel POST sur un endpoint référencé dans API_ENDPOINTS."""
        path = API_ENDPOINTS[endpoint_key]
        return await self._request("POST", path, payload=payload or {})

    async def async_test_credentials(self) -> dict[str, Any]:
        """Test de connexion utilisé par le config_flow.

        Renvoie les infos système si la connexion fonctionne,
        lève une exception sinon.
        """
        return await self.get("system_information")

    async def async_get_all(self) -> dict[str, dict[str, Any]]:
        """Récupère toutes les données en parallèle pour le coordinator.

        Renvoie un dict avec une clé par endpoint. Si un endpoint échoue,
        sa valeur sera None et l'erreur est loggée (les autres continuent).
        """
        keys = [
            "firmware_status",
            "system_information",
            "system_resources",
            "system_disk",
            "system_time",
            "cpu_type",
            "interfaces",
            "traffic_totals",
            "traffic_wan",
        ]
        tasks = [self.get(key) for key in keys]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        data: dict[str, dict[str, Any] | None] = {}
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, OPNsenseAuthError):
                # Clé API invalide : erreur fatale, on remonte pour déclencher
                # le flux de ré-authentification côté coordinator.
                raise result
            if isinstance(result, OPNsenseForbiddenError):
                # Privilège manquant sur CET endpoint : on dégrade proprement
                # (les autres capteurs continuent de fonctionner).
                _LOGGER.warning(
                    "Privilège manquant pour '%s' côté OPNsense: %s", key, result
                )
                data[key] = None
            elif isinstance(result, Exception):
                _LOGGER.warning(
                    "Échec de récupération de '%s': %s", key, result
                )
                data[key] = None
            else:
                data[key] = result

        # NB : on ne lève pas ici si tout est None. Le coordinator vérifie
        # `system_information` et remonte un UpdateFailed explicite
        # ("vérifier les privilèges"), message plus juste qu'un "injoignable".
        return data

    async def async_check_for_updates(self) -> dict[str, Any]:
        """Force OPNsense à vérifier la disponibilité de mises à jour.

        Endpoint POST. Après l'appel, /firmware/status renverra les
        infos à jour au prochain polling.
        """
        return await self.post("firmware_check")

    async def async_run_update(self) -> dict[str, Any]:
        """Déclenche la mise à jour du firmware.

        Attention : opération longue côté OPNsense, redémarrage possible.
        """
        return await self.post("firmware_update")
