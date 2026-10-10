<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim : récupération de données scientifiques HDF5" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="Licence : Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="docs/coverage.md"><img alt="Évaluation contrôlée : 95,4 % de récupérations utiles" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>Langues:</strong>
  <a href="README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

<!-- English README SHA-256: 5a2c704dd8e97c5bb668d1c2f9e856b9e21568e44477487b74e281f694a66646 -->

H5Reclaim récupère les données scientifiques de fichiers HDF5 endommagés. Il rassemble les mesures encore lisibles dans un nouveau fichier exploitable après une écriture interrompue, la rupture d'un index, la détérioration des métadonnées ou une troncation. De nombreux instruments et logiciels scientifiques utilisent le format HDF5 pour stocker des tableaux, des mesures et leurs métadonnées.

**Une seule commande découvre vos jeux de données, choisit les méthodes de récupération et crée un nouveau fichier HDF5 accompagné d'un rapport JSON clair.** Le fichier d'origine reste intact. H5Reclaim conserve les mesures lisibles des jeux de données partiellement endommagés et poursuit la récupération du reste du fichier.

La version candidate 1.0.0rc1 a obtenu **95,4 % de récupérations utiles sur 109 essais contrôlés avec des fichiers endommagés** : 77 récupérations entièrement exactes et 27 récupérations partielles, sans aucune valeur erronée acceptée dans cet ensemble d'essais. Ces essais générés mesurent les catégories de défauts déclarées, et ne constituent pas une estimation du taux de réussite sur d'autres fichiers. [Consulter les résultats](docs/coverage.md).

[Démarrage rapide](#quick-start) · [Guide d'utilisation](docs/usage.md) · [Couverture de la récupération](docs/coverage.md) · [Format du rapport](docs/report-schema.md) · [Notes de version](CHANGELOG.md)

<a id="quick-start"></a>
## Démarrage rapide

Vous avez besoin de Python 3.10 ou d'une version plus récente. Clonez ce dépôt, ou téléchargez et extrayez son archive ZIP depuis GitHub :

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
python -m venv .venv
```

Activez l'environnement :

| Système | Commande |
| --- | --- |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows Command Prompt | `.venv\Scripts\activate.bat` |
| macOS / Linux | `. .venv/bin/activate` |

Installez le projet depuis la racine du dépôt et récupérez un fichier :

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

Cette commande crée `damaged.recovered.h5` et `damaged.recovered.report.json` à côté du fichier source. Les destinations existantes ne sont jamais écrasées. Indiquez explicitement les chemins pour choisir un autre emplacement ou relancer une récupération :

```sh
python -m h5reclaim rescue "damaged.h5" --output "rescued.h5" --report "evidence.json"
```

La commande `h5reclaim` installée fonctionne également. Lancez `python -m h5reclaim --help` pour afficher les commandes, ou `python -m h5reclaim rescue --help` pour les options de récupération.

## Comprendre le résultat

Le résumé affiché dans le terminal indique si la récupération est complète ou partielle et où se trouvent les fichiers produits. Le rapport JSON répertorie les jeux de données récupérés, les méthodes utilisées, les positions non résolues et les métadonnées restaurées.

Vérifiez le fichier produit à l'aide du rapport enregistré avant de l'utiliser :

```sh
python -m h5reclaim verify-result "damaged.recovered.h5" "damaged.recovered.report.json" --source "damaged.h5"
```

Cette commande vérifie la cohérence du rapport, de la forme des jeux de données, de l'empreinte du fichier source et de la carte de validité dans un processus aux ressources limitées. Elle ne lit ni n'authentifie les valeurs des mesures récupérées ; les méthodes dépourvues de carte vérifiable renvoient un résultat « non pris en charge ». Le [guide d'utilisation](docs/usage.md#verify-a-published-result) précise les codes de sortie et les limites.

Lorsqu'une méthode produit une **carte d'état**, celle-ci indique les positions contenant des mesures acceptées. Utilisez la carte indiquée dans le rapport pour sélectionner les valeurs destinées à l'analyse ; les positions inconnues peuvent afficher une valeur de remplissage, telle que zéro. Certains jeux de données contigus intacts et certains jeux de données nuls ne disposent pas de carte vérifiable : consultez alors le rapport propre à leur méthode avant l'analyse. Les références antérieures et les ensembles de protection permettent une comparaison avec une capture précédente.

Pour les jeux de données de taille fixe, lisez les valeurs récupérées avec les positions inconnues déjà masquées :

```python
from h5reclaim import read_masked

values = read_masked(
    "damaged.recovered.h5", "damaged.recovered.report.json",
    "/experiment/readings", selection=(slice(0, 1000), Ellipsis),
)
```

Cette fonction de lecture à ressources limitées utilise la carte d'état mentionnée dans le rapport du jeu de données. Le masque désigne les valeurs acceptées à partir du fichier endommagé ; il ne prouve pas qu'elles correspondent à une capture antérieure. Consultez le [guide d'utilisation](docs/usage.md#read-results) pour connaître les sélections prises en charge et les limites.

## Utilisations courantes

| Objectif | Commande |
| --- | --- |
| Examiner un fichier avant la récupération | `python -m h5reclaim diagnose damaged.h5` |
| Récupérer un jeu de données | `python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings` |
| Résumer un résultat existant | `python -m h5reclaim report damaged.recovered.report.json` |
| Vérifier la cohérence du fichier produit et du rapport | `python -m h5reclaim verify-result damaged.recovered.h5 damaged.recovered.report.json` |
| Reprendre une récupération longue | `python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress` |
| Fournir des fichiers associés | `python -m h5reclaim rescue container.h5 --related-dir companion-files` |
| Retrouver des jeux de données dont le nom est perdu | `python -m h5reclaim discover damaged.h5 --json` |

Certains fichiers nécessitent des codecs de compression supplémentaires. Installez `python -m pip install ".[filters]"` depuis la racine du dépôt, puis réessayez. Le [guide d'utilisation](docs/usage.md) traite des fichiers volumineux, des limites de ressources, des fichiers dépendants, de la reprise des opérations et des ensembles de protection antérieurs.

## Couverture de la récupération

H5Reclaim lit les descriptions des jeux de données dans chaque fichier : le même processus fonctionne ainsi avec différents protocoles expérimentaux, noms de jeux de données et formes de tableaux.

| Domaine | Prise en charge |
| --- | --- |
| Stockage | Jeux de données compacts, contigus et découpés en blocs ; index de blocs anciens et récents |
| Valeurs | Tableaux numériques, enregistrements composés, chaînes de taille fixe ou variable, tableaux irréguliers, énumérations, références, jeux de données vides et nuls |
| Dommages | Récupération de liens d'index rompus, corrections des métadonnées et des dimensions des blocs justifiées par les sommes de contrôle, réparation des signatures récentes, indicateurs d'écriture interrompue, en-têtes de jeux de données survivants, blocs illisibles et troncation physique de la fin du fichier |
| Compression | DEFLATE, LZF, shuffle, Fletcher32 et codecs optionnels fournis et pris en charge |
| Structure du fichier | Groupes, attributs, liens, types nommés, références, échelles de dimensions et en-têtes applicatifs disponibles |
| Dépendances | Stockage externe, jeux de données virtuels, liens externes et fichiers Family/Split avec fichiers associés ou manifestes explicitement fournis |

Pour les acquisitions protégées avant les dommages, les répliques conservées et la parité peuvent également reconstruire les données manquantes. La découverte des fichiers associés, la reprise des opérations et les limites de traitement en flux facilitent le travail sur des fichiers scientifiques volumineux.

L'**évaluation de récupération automatique 1.0.0rc1** couvre 23 familles générées de données et d'agencements. Ses **109 essais contrôlés avec des fichiers endommagés** ont donné 77 récupérations entièrement exactes, 27 partielles et 5 refus : **104 résultats utiles (95,4 %)**. Le programme a récupéré 83,1 % des éléments d'origine à leurs coordonnées exactes, sans valeur erronée acceptée ni fichier source modifié. Les 15 défauts déclarés de dimensions de blocs ont donné des résultats utiles, y compris pour les tableaux de rang cinq, scale-offset, les chaînes de taille variable, les tableaux irréguliers et les champs composés de taille variable. [Voir le rapport complet des cas](benchmarks/results/v100rc1-release-coverage.json) ou la [répartition de la couverture](docs/coverage.md).

Les évaluations v0.14.0 enregistrées comprennent :

| Évaluation | Résultat enregistré |
| --- | --- |
| [Quatre fichiers scientifiques intacts](benchmarks/results/v014-scientific-whole.json) | 251 jeux de données et 253 attributs comparés exactement avec les originaux conservés |
| [Lien d'index GWOSC rompu de manière contrôlée](benchmarks/results/v014-gwosc-controlled.json) | 128/128 blocs récupérés à leurs coordonnées exactes ; aucun bloc erroné |
| [MATLAB 7.3, netCDF4 et NWB](benchmarks/results/v014-application-readers.json) | Des lecteurs indépendants ont ouvert six résultats intacts ou issus de dommages contrôlés ; aucune valeur erronée acceptée, les zones endommagées étant laissées inconnues |

Le [guide des évaluations](benchmarks/README.md) explique comment reproduire ces essais. Les [notes sur le corpus](corpus/README.md) attribuent les fichiers scientifiques à leurs auteurs.

## Contribuer et signaler un problème

Ouvrez une [issue](https://github.com/xShadyy/H5Reclaim/issues) en indiquant la commande, les versions de l'outil et de Python, l'erreur observée et le résultat attendu. Un petit fichier permettant de reproduire le problème et son rapport aident à distinguer un stockage non pris en charge d'un défaut de récupération.

Pour signaler une vulnérabilité, suivez la [politique de signalement des problèmes de sécurité](SECURITY.md) ; vérifiez que les rapports ne contiennent pas de chemins ou de métadonnées sensibles avant de les partager.

Pour contribuer au développement, installez `python -m pip install -e ".[filters]"` et exécutez :

```sh
python -m unittest discover -s tests -q
```

Les tests vérifient l'exactitude de la récupération, la préservation des fichiers sources et le traitement des données non résolues. Les évaluations comparent les valeurs acceptées à des originaux conservés séparément. Le [guide du dépôt](docs/file-guide.md) présente l'implémentation et la raison d'être des fichiers.
Le [guide de publication](docs/releasing.md) décrit la compilation de la version candidate, les vérifications liées à la balise et les contrôles restant avant la version stable 1.0.

## Licence

Le code source est disponible sous [licence Apache 2.0](LICENSE). Les fichiers scientifiques inclus ont des [attributions et licences distinctes](corpus/README.md).

<sub>Créé par Tymoteusz Netter.</sub>
