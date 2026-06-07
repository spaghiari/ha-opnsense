# OPNsense pour Home Assistant

[![GitHub Release][releases-shield]][releases]
[![License][license-shield]][license]
[![hacs][hacs-shield]][hacs]
[![Code style: black][black-shield]][black]

> 🇬🇧 **English version: [README.md](README.md)**

Une intégration custom Home Assistant qui expose votre **firewall OPNsense** sous forme d'appareil natif avec de nombreux capteurs, une vraie entité `update` pour gérer le firmware, et un accès API à privilèges minimaux.

![Aperçu de l'intégration](https://raw.githubusercontent.com/spaghiari/ha-opnsense/main/.github/screenshot.png)

---

## ✨ Fonctionnalités

### Monitoring système
- **Charge CPU** - moyennes 1/5/15 min
- **RAM** - totale, utilisée, % utilisé
- **Disque** - partition racine avec %
- **Uptime + date du dernier démarrage**
- **Hostname** + **modèle CPU**
- **Versions OPNsense / FreeBSD / OpenSSL**

### Monitoring WAN
- **Adresses IP publiques** IPv4 et IPv6
- **Débit temps réel** (entrée/sortie en Mbps)
- **Total transféré** (entrée/sortie en GB, type `TOTAL_INCREASING` compatible avec `utility_meter`)
- **Top 5 destinations** avec résolution DNS inverse
- **Connectivité WAN** (binary sensor)

### Gestion firmware
- **Entité `update` native** - compare version installée vs disponible, bouton "Installer" en un clic
- **Bouton "Vérifier les mises à jour"** pour forcer un check à la demande
- **Binary sensor `update_available`** prêt pour automatisations

### Configuration
- **Installation via UI** - pas de YAML
- **Intervalle de polling configurable** à chaud (30 à 600 secondes)
- **Interface multilingue** (français, anglais)

---

## 📋 Prérequis

### Versions supportées
- Home Assistant Core **2024.4.0** ou plus récent
- OPNsense **26.1** ou plus récent (versions antérieures peuvent fonctionner mais non testées)

### Configuration OPNsense

Vous devez créer un **utilisateur API dédié avec privilèges minimaux** dans OPNsense. **N'utilisez jamais le compte `root`**.

#### 1. Créer un groupe
**System → Access → Groups → +**, créer un groupe avec ces **8 privilèges** :

| Privilège | Utilisé pour |
|---|---|
| `Lobby: Dashboard` | Accès API de base |
| `Diagnostics: ARP Table` | État réseau |
| `Diagnostics: Show States` | Table de connexions |
| `Diagnostics: System Activity` | Infos CPU/processus |
| `Status: Interfaces` | Détails des interfaces |
| `System: Firmware` | Version firmware + mises à jour |
| `System: Status` | Endpoint infos système |
| `Reporting: Traffic` | Capteurs débit WAN |

> ⚠️ **Ne cochez PAS "All pages"** - ça annulerait l'intérêt d'un utilisateur restreint.

#### 2. Créer un utilisateur
**System → Access → Users → +** :
- Username : `homeassistant`
- Password : 32 caractères aléatoires (jamais utilisé, juste requis par le formulaire)
- Login shell : `Default (none for all but root)` - **pas d'accès SSH**
- Group membership : ajouter le groupe créé précédemment

#### 3. Générer une clé API
Dans la liste des utilisateurs, cliquer sur l'**icône "carte"** à côté de `homeassistant`. Un fichier `apikey.txt` se télécharge automatiquement. **Ouvrez-le une fois et sauvegardez-le en sécurité** - il contient :

```
key=...
secret=...
```

Le secret n'est affiché qu'à la génération. Si perdu = régénérer.

---

## 🚀 Installation

### Option A - HACS (recommandé)

1. Dans HACS, allez dans **Intégrations → ⋮ → Dépôts personnalisés**
2. Ajoutez `https://github.com/spaghiari/ha-opnsense` en type `Intégration`
3. Cherchez **OPNsense** dans la liste et cliquez **Télécharger**
4. **Redémarrez Home Assistant**

### Option B - Manuelle

1. Téléchargez le ZIP de la dernière release depuis [releases][releases]
2. Extrayez `custom_components/opnsense_custom/` dans votre dossier HA `config/custom_components/`
3. Le chemin final doit être `config/custom_components/opnsense_custom/__init__.py`
4. **Redémarrez Home Assistant**

---

## ⚙️ Configuration

1. **Paramètres → Appareils et services → + Ajouter une intégration**
2. Cherchez **OPNsense**
3. Remplissez le formulaire :
   - **Host** : IP de votre OPNsense (ex. `192.168.1.1`)
   - **Port** : `443` (HTTPS par défaut)
   - **Clé API** : la valeur `key=` de votre `apikey.txt`
   - **Secret API** : la valeur `secret=`
   - **Vérifier le certificat SSL** : laisser **décoché** si vous utilisez le certificat auto-signé d'OPNsense (cas par défaut)
4. Cliquez **Valider**
5. **Choisissez l'interface WAN** à surveiller (débit, IP publique, connectivité).
   L'intégration pré-sélectionne celle auto-détectée - laissez sur
   **Auto-détection** sauf installation non standard / multi-WAN.

L'intégration teste la connexion. En cas de succès, un appareil **OPNsense** apparaît avec ~30 entités.

### Intervalle de polling & interface WAN

Modifiez la fréquence de polling et l'interface WAN à tout moment, sans
réinstaller :

**Paramètres → Appareils et services → OPNsense → ⚙ Configurer** → intervalle
(30-600 s) et interface WAN.

### Si votre clé API change

Clé tournée ou révoquée dans OPNsense ? Home Assistant déclenche
automatiquement une invite de **ré-authentification** - saisissez la nouvelle
clé/secret et l'intégration se recharge. Vous pouvez aussi utiliser
**⋮ → Reconfigurer** pour changer l'hôte/port.

---

## 🖥️ Dashboard prêt à l'emploi (Glass NOC)

**Automatique** - à l'installation, l'intégration crée un dashboard **OPNsense**
soigné dans la barre latérale : hero glassmorphism, chips d'état, jauges
circulaires RAM/disque/CPU, graphe de débit WAN et top destinations. Il est
construit à partir de tes **vrais entity_id**, donc il fonctionne quelle que
soit la langue de ton Home Assistant. Désactivable à tout moment via
**OPNsense → ⚙ Configurer → Créer un dashboard OPNsense dans la barre latérale**.

### Prérequis frontend (cartes HACS)

Le dashboard par défaut utilise ces cartes custom - installe-les depuis
**HACS → Frontend** (une fois) pour le rendu prévu :

| Carte | Nom HACS |
|---|---|
| Mushroom | `Mushroom` |
| ApexCharts Card | `apexcharts-card` |
| Mini Graph Card | `mini-graph-card` |
| card-mod | `card-mod` |
| Stack In Card | `stack-in-card` |

Si une carte manque, l'intégration loggue un avertissement (filtre
`opnsense_custom`) et la carte s'affiche en « Custom element doesn't exist » -
installe-la puis recharge.

> **Dashboard géré.** La mise en page est rafraîchie quand l'intégration livre
> une nouvelle version de gabarit : tes éditions manuelles dessus peuvent être
> écrasées lors d'une mise à jour. Pour personnaliser librement, désactive
> l'option et duplique le dashboard, ou pars de
> [`dashboards/opnsense.yaml`](dashboards/opnsense.yaml).

---

## 📊 Liste des entités

### Activées par défaut
- `sensor.opnsense_hostname`
- `sensor.opnsense_version_opnsense`
- `sensor.opnsense_charge_cpu_1_min`
- `sensor.opnsense_ram_utilisee_percent`
- `sensor.opnsense_disque_root_percent`
- `sensor.opnsense_uptime`
- `sensor.opnsense_dernier_demarrage`
- `sensor.opnsense_ip_publique_ipv4`
- `sensor.opnsense_debit_wan_entrant/sortant`
- `sensor.opnsense_total_recu/transmis_wan`
- `sensor.opnsense_top_destination_entrante/sortante`
- `binary_sensor.opnsense_mise_a_jour_disponible`
- `binary_sensor.opnsense_wan_connecte`
- `update.opnsense_firmware`
- `button.opnsense_verifier_les_mises_a_jour`

### Désactivées par défaut (à activer manuellement si besoin)
- Versions FreeBSD / OpenSSL
- Charge CPU 5/15 min
- Capteurs disque détaillés (total, utilisé, disponible)
- RAM totale
- Statut WAN (textuel)
- Modèle CPU
- IPv6 publique
- Firmware installé/disponible (textuel)

---

## 💡 Exemples d'utilisation

### Notification quand une MAJ est disponible

```yaml
alias: "OPNsense - notification mise à jour"
trigger:
  - platform: state
    entity_id: binary_sensor.opnsense_mise_a_jour_disponible
    to: "on"
action:
  - service: notify.mobile_app_VOTRE_TELEPHONE
    data:
      title: "🛡️ OPNsense"
      message: "Une mise à jour firmware est disponible."
```

### Suivi de conso mensuelle WAN avec utility_meter

```yaml
# configuration.yaml
utility_meter:
  wan_conso_mensuelle_download:
    source: sensor.opnsense_total_recu_wan
    name: "Conso WAN mensuelle - Download"
    cycle: monthly
  wan_conso_mensuelle_upload:
    source: sensor.opnsense_total_transmis_wan
    name: "Conso WAN mensuelle - Upload"
    cycle: monthly
```

### Carte Top destinations (Markdown)

```yaml
type: markdown
title: Top destinations WAN (download)
content: |
  {%- set top = state_attr('sensor.opnsense_top_destination_entrante', 'top_5') %}
  {%- if top %}
  {%- for d in top %}
  **#{{ loop.index }}** - `{{ d.name }}` - **{{ d.rate_mbps }} Mbps**
  {% endfor %}
  {%- else %}
  *Pas de trafic significatif*
  {%- endif %}
```

---

## 🔒 Sécurité

- L'utilisateur API n'a que les **8 privilèges minimums** listés ci-dessus (lecture seule + mise à jour firmware)
- **Aucun accès en écriture** aux règles firewall, interfaces, ou comptes
- **Pas de shell, pas de SSH** pour le compte API
- Les identifiants sont stockés chiffrés par Home Assistant
- Si votre clé API fuit, **révoquez-la et régénérez-en une** depuis `System → Access → Users → ApiKeys`, puis **Reconfigurer** l'intégration avec la nouvelle clé

---

## 🐛 Dépannage

### Erreur "Impossible de se connecter" à l'installation
- Vérifier que HA peut joindre OPNsense (`ping` depuis un terminal HA)
- Vérifier le port (443 par défaut)
- Tester la clé API manuellement :
  ```bash
  curl -sk -u "KEY:SECRET" https://<IP_OPNSENSE>/api/diagnostics/system/system_information
  ```

### Certains capteurs restent "Indisponible"
Cela veut généralement dire qu'un **privilège manque** côté OPNsense. Vérifiez les logs HA :

**Paramètres → Système → Journaux**, filtre sur `opnsense_custom`. Vous verrez des lignes du genre :
```
Échec de récupération de 'XXX': ... 403 ...
```
→ Ajoutez le privilège correspondant au groupe `homeassistant`.

### Erreur "Privilèges insuffisants"
Pareil - votre utilisateur API n'a pas un des 8 privilèges requis. Ajoutez celui qui manque et réessayez.

---

## 🤝 Contribuer

PRs et issues bienvenus ! Merci de :
- Ouvrir une issue d'abord pour bugs / demandes de fonctionnalités
- Suivre le style de code [black][black] pour Python
- Tester sur votre propre OPNsense avant de soumettre

### Signaler un bug
Pour un rapport de bug, incluez :
- Version Home Assistant Core
- Version OPNsense
- Version de l'intégration (dans `manifest.json`)
- Logs pertinents (filtre sur `opnsense_custom`)

---

## ⚠️ Notes importantes

- **Faites toujours un backup XML de votre OPNsense** (System → Configuration → Backups) avant de déclencher une mise à jour firmware depuis Home Assistant
- L'intégration n'utilise **que des endpoints API documentés** - pas de SSH, pas de modification XML
- **Les métriques Zenarmor** ne sont pas encore supportées (Zenarmor nécessite une API séparée via Zenconsole, prévue dans une future intégration compagne)

---

## 📜 Licence

MIT - voir [LICENSE](LICENSE).

Cette intégration n'est **pas affiliée à Deciso B.V.** (éditeur d'OPNsense). "OPNsense" est une marque déposée de Deciso B.V.

---

## 🙏 Crédits

- L'équipe OPNsense pour leur API REST robuste
- La communauté Home Assistant

---

[releases-shield]: https://img.shields.io/github/v/release/spaghiari/ha-opnsense?style=flat-square
[releases]: https://github.com/spaghiari/ha-opnsense/releases
[license-shield]: https://img.shields.io/github/license/spaghiari/ha-opnsense?style=flat-square
[license]: LICENSE
[hacs-shield]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat-square
[hacs]: https://hacs.xyz
[black-shield]: https://img.shields.io/badge/code%20style-black-000000.svg?style=flat-square
[black]: https://github.com/psf/black
