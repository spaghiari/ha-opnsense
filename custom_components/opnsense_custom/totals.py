"""Cumul monotone des compteurs d'octets des interfaces WAN."""
from __future__ import annotations


def accumulate(
    baselines: dict[str, int],
    counters: dict[str, int],
    acc: float | int,
) -> tuple[float | int, dict[str, int]]:
    """Ajoute au cumul les deltas positifs des compteurs, interface par interface.

    `baselines` : dernier compteur vu par interface (clé = device, ex: igc0).
    `counters`  : compteurs actuels des interfaces présentes dans ce cycle.
    `acc`       : cumul courant, qui ne décroît jamais.

    - Compteur >= référence : delta = compteur - référence.
    - Compteur < référence (reset de l'interface, reboot) : delta = compteur,
      le compteur ayant repris depuis zéro.
    - Interface sans référence (première vue) : delta = 0, on pose la référence.
    - Interface absente du cycle (reconnexion, lien disparu) : elle garde sa
      référence, rien n'est retiré ni compté.

    Renvoie (nouveau cumul, nouvelles références) ; les entrées ne sont pas
    modifiées.
    """
    new_baselines = dict(baselines)
    for device, value in counters.items():
        previous = baselines.get(device)
        if previous is not None:
            acc += value - previous if value >= previous else value
        new_baselines[device] = value
    return acc, new_baselines
