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
- **Températures** *(v2.1)* - CPU (sonde la plus chaude) et modules SFP, détail de chaque sonde en attributs

### Monitoring WAN
- **Adresses IP publiques** IPv4 et IPv6
- **Débit temps réel** (entrée/sortie en Mbps)
- **Total transféré** (entrée/sortie en GB, type `TOTAL_INCREASING` compatible avec `utility_meter`)
- **Top 5 destinations** avec résolution DNS inverse
- **Connectivité WAN** (binary sensor)

### Santé de la connexion *(v1.7)*
- **Latence et pertes de paquets WAN** issues du moniteur de passerelles d'OPNsense (dpinger)
- **Services arrêtés** : compteur + liste des services à l'arrêt
- **Tunnels VPN** (WireGuard / IPsec / OpenVPN) : nombre de tunnels actifs et détail par tunnel - aucun privilège supplémentaire
- Le WAN n'a plus besoin de s'appeler `wan` dans OPNsense pour le débit temps réel / top destinations

### Multi-WAN et groupes de passerelles *(v2.3)*
- **Un WAN = une passerelle montante** (case *Upstream Gateway* d'OPNsense) : les passerelles VPN ne sont jamais prises pour un WAN, et les passerelles IPv4 / IPv6 d'une même interface forment un seul lien
- **Panne opérateur détectée** : un lien est coupé si son interface est down **ou** si sa passerelle est hors ligne / à 100 % de pertes (avant la v2.3, seul l'état de l'interface comptait)
- **WAN connecté** = au moins un lien en ligne ; latence et pertes suivent le lien qui porte la route par défaut ; débit et volumes = somme des liens
- **Avec deux WAN ou plus** : un jeu d'entités par lien (*\<lien\> connecté*, latence, pertes, débit entrant / sortant), créé automatiquement
- **Groupes de passerelles** : un capteur par groupe, dont l'état est la passerelle qui porte le trafic, avec les membres par niveau en attributs (OPNsense récent, privilège optionnel)
- **WAN à ignorer** : dans les options, pour écarter un lien (ex. une 4G de secours)
- **Dashboard** : section « Liens WAN » (une carte par lien : état, latence, pertes, débits ; une carte par groupe avec la passerelle qui porte le trafic) et bandeau « via <lien> » ; masquée avec un seul WAN

### Gestion firmware
- **Entité `update` native** - compare version installée vs disponible, bouton "Installer" en un clic
- **Bouton "Vérifier les mises à jour"** pour forcer un check à la demande
- **Binary sensor `update_available`** prêt pour automatisations

### Configuration
- **Installation via UI** - pas de YAML
- **Menu de configuration** en trois écrans *(v2.1)* : rafraîchissement, dashboard et thème, notifications (curseurs avec unités, seuils repliables)
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
| `System: Gateways` *(optionnel, `Status: Gateways` sur les anciennes versions)* | Liens WAN, panne opérateur, latence et pertes de paquets |
| `System: Gateway Groups` *(optionnel)* | Capteurs des groupes de passerelles et alertes de bascule |
| `Status: Services` *(optionnel)* | Capteur des services arrêtés |

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

### Menu de configuration

**Paramètres → Appareils et services → OPNsense → ⚙ Configurer** ouvre un menu
à trois écrans, modifiables à chaud sans réinstaller :

| Écran | Réglages |
|---|---|
| ⚡ Rafraîchissement et interface WAN | temps réel on/off, cadence du temps réel, polling rapide (2-60 s), polling lent (30-600 s), interface WAN |
| 🎨 Dashboard et thème | création du dashboard, choix du thème (Graphite, Aurore, Cockpit) |
| 🔔 Notifications et alertes | alertes, destinataires, notification persistante, et une section **Seuils** repliée (délai WAN, latence, température) |

Chaque écran n'enregistre que ses propres réglages ; les autres sont conservés.

### Si votre clé API change

Clé tournée ou révoquée dans OPNsense ? Home Assistant déclenche
automatiquement une invite de **ré-authentification** - saisissez la nouvelle
clé/secret et l'intégration se recharge. Vous pouvez aussi utiliser
**⋮ → Reconfigurer** pour changer l'hôte/port.

---

## 🖥️ Dashboard prêt à l'emploi, trois thèmes

**Automatique** - à l'installation, l'intégration crée un dashboard **OPNsense**
façon pupitre de supervision dans la barre latérale : bandeau d'état qui vire au rouge si le WAN tombe,
bande « Temps réel » (débit, latence, CPU) avec sparklines, courbe de trafic
WAN sur 24 h, pertes, services et firmware, **températures** CPU / SFP avec
jauge colorée, RAM et disque en jauges, top destinations classées avec barres
au prorata, tunnels VPN et volumes WAN.

Trois thèmes au choix dans **⚙ Configurer → Dashboard et thème** (le
dashboard est régénéré aussitôt) :

| Thème | Style |
|---|---|
| **Graphite** *(par défaut)* | sobre et mat, une seule couleur vive (orange OPNsense) |
| **Aurore** | lueurs dégradées en arrière-plan, cartes en verre doux |
| **Cockpit** | instruments, gros chiffres en monospace, ambre et cyan |

Un état n'est jamais porté par la seule couleur : il est toujours doublé d'un
texte ou d'une icône. Chaque
pare-feu a son propre dashboard (`opnsense`, `opnsense-2`...). Il est
construit à partir de tes **vrais entity_id**, donc il fonctionne quelle que
soit la langue de ton Home Assistant. Désactivable à tout moment via
**OPNsense → ⚙ Configurer → Créer un dashboard OPNsense dans la barre latérale**.

### Prérequis frontend (cartes HACS)

Le dashboard par défaut utilise ces cartes custom - installe-les depuis
**HACS → Frontend** (une fois) pour le rendu prévu :

| Carte | Nom HACS |
|---|---|
| button-card | `button-card` |
| ApexCharts Card | `apexcharts-card` |
| Mini Graph Card | `mini-graph-card` |
| card-mod | `card-mod` |

Si une carte manque, l'intégration loggue un avertissement (filtre
`opnsense_custom`) et la carte s'affiche en « Custom element doesn't exist » -
installe-la puis recharge.

> **Dashboard géré.** La mise en page est rafraîchie quand l'intégration livre
> une nouvelle version de gabarit : tes éditions manuelles dessus peuvent être
> écrasées lors d'une mise à jour. Pour personnaliser librement, désactive
> l'option et duplique le dashboard, ou pars de
> [`dashboards/opnsense.yaml`](dashboards/opnsense.yaml).

---

## ⚡ Rafraîchissement à trois vitesses (v2)

| Vitesse | Données | Mécanisme |
|---|---|---|
| **Temps réel** (~1 s, publié toutes les 2 s par défaut) | Débit WAN, **CPU %** | Les flux continus d'OPNsense (ceux de son propre dashboard) : une connexion ouverte, OPNsense pousse les valeurs, sans polling |
| **Rapide** (10 s par défaut) | État du WAN, IP publiques, latence / pertes, tunnels VPN, compteurs d'octets | Polling léger de quelques endpoints |
| **Lent** (60 s par défaut) | Firmware, système, disque, RAM, services, top destinations | Polling classique |

- Le débit est une **moyenne exacte** (octets × 8 / temps écoulé), jamais un instantané.
- Si un flux est indisponible (droits, réseau), le débit se rabat sur les
  compteurs du polling rapide, sans erreur ni avalanche de logs.
- Les écritures d'état sont limitées à l'intervalle de publication : la base de
  l'historique ne grossit pas à chaque événement reçu.
- Tout se règle dans **OPNsense → ⚙ Configurer** (temps réel on/off, fréquence
  de publication, intervalles rapide et lent). Le diagnostic téléchargeable
  indique l'état des flux temps réel.

## 🔔 Alertes intégrées

**OPNsense → ⚙ Configurer → Notifications** : coche les alertes voulues et
choisis ton ou tes téléphones. Aucune automatisation à écrire.

| Alerte | Envoyée quand |
|---|---|
| WAN coupé / rétabli | WAN coupé plus longtemps que le délai (1 min par défaut) ; le message de retour indique la durée de la coupure. En multi-WAN : un message par lien, « Internet coupé » quand tous les liens sont tombés, et un message à chaque bascule d'un groupe de passerelles (ou de la route par défaut) |
| Latence élevée | Latence au-dessus du seuil (100 ms) pendant une durée (5 min), puis retour à la normale |
| Mise à jour firmware | Une nouvelle version d'OPNsense est disponible |
| Service arrêté / relancé | Un service s'arrête (ceux déjà arrêtés au démarrage sont ignorés) |
| Tunnel VPN coupé / rétabli | Un tunnel WireGuard / IPsec / OpenVPN change d'état |
| Disque presque plein | Partition racine au-dessus de 90 % (réarmé sous 85 %) |
| Température CPU élevée | CPU au-dessus du seuil (80 °C par défaut), puis retour sous le seuil moins 5 °C |

> 💡 Pendant une coupure du WAN, une notification push ne peut pas sortir de
> chez toi (elle passe par Apple/Google), sauf en **local push** de l'app
> Companion sur le Wi-Fi de la maison. Le message « WAN rétabli », lui, arrive
> toujours, avec la durée de la coupure.

### Avancé : blueprint

[![Importer le blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/create-link/?redirect=blueprint_import&blueprint_url=https%3A%2F%2Fgithub.com%2Fspaghiari%2Fha-opnsense%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fopnsense_custom%2Fopnsense_alerts.yaml)

Prévient quand le WAN tombe (après un délai réglable), quand il revient, et
quand la latence reste au-dessus d'un seuil. Par défaut, crée une notification
persistante dans Home Assistant ; remplace l'action par une notification
mobile si tu préfères (`{{ title }}` / `{{ message }}` sont disponibles).

---

## 📊 Liste des entités

### Activées par défaut
- `sensor.opnsense_hostname`
- `sensor.opnsense_version_opnsense`
- `sensor.opnsense_charge_cpu_1_min`
- `sensor.opnsense_ram_utilisee_percent`
- `sensor.opnsense_disque_root_percent`
- `sensor.opnsense_temperature_cpu` / `sensor.opnsense_temperature_sfp` *(v2.1)*
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
