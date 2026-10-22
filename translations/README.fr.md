<p align="center">
  <img src="../assets/h5reclaim-banner.png" alt="H5Reclaim : récupération de données scientifiques HDF5" width="760">
</p>

<p align="center">
  <a href="../pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="../LICENSE"><img alt="Licence : Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json"><img alt="Évaluation contrôlée : 95,4 % de récupérations utiles" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>Langues:</strong>
  <a href="../README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

H5Reclaim récupère les données scientifiques de fichiers HDF5 endommagés. Il rassemble les mesures encore lisibles dans un nouveau fichier exploitable après une écriture interrompue, la rupture d'un index, la détérioration des métadonnées ou une troncation. De nombreux instruments et logiciels scientifiques utilisent le format HDF5 pour stocker des tableaux, des mesures et leurs métadonnées.

**Une seule commande découvre vos jeux de données, choisit les méthodes de récupération et crée un nouveau fichier HDF5 accompagné d'un rapport JSON clair.** Le fichier d'origine reste intact. H5Reclaim conserve les mesures lisibles des jeux de données partiellement endommagés et poursuit la récupération du reste du fichier.

La version candidate 1.0.0rc1 a obtenu **95,4 % de récupérations utiles sur 109 essais contrôlés avec des fichiers endommagés** : 77 récupérations entièrement exactes et 27 récupérations partielles, sans aucune valeur erronée acceptée dans cet ensemble d'essais. Ces essais générés mesurent les catégories de défauts déclarées, et ne constituent pas une estimation du taux de réussite sur d'autres fichiers. [Consulter les résultats](https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json).

<a id="quick-start"></a>
## Démarrage rapide

Vous avez besoin de Python 3.10 ou d'une version plus récente. Clonez ce dépôt, ou téléchargez et extrayez son archive ZIP depuis GitHub :

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
```

Installez le projet depuis la racine du dépôt et récupérez un fichier :

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

## Comprendre le résultat

Le résumé affiché dans le terminal indique si la récupération est complète ou partielle et où se trouvent les fichiers produits. Le rapport JSON répertorie les jeux de données récupérés, les méthodes utilisées, les positions non résolues et les métadonnées restaurées.

Lorsqu'une méthode produit une **carte d'état**, celle-ci indique les positions contenant des mesures acceptées. Utilisez la carte indiquée dans le rapport pour sélectionner les valeurs destinées à l'analyse ; les positions inconnues peuvent afficher une valeur de remplissage, telle que zéro. Certains jeux de données contigus intacts et certains jeux de données nuls ne disposent pas de carte vérifiable : consultez alors le rapport propre à leur méthode avant l'analyse. Les références antérieures et les ensembles de protection permettent une comparaison avec une capture précédente.

## Licence

Le code source est disponible sous [licence Apache 2.0](../LICENSE). Les fichiers scientifiques inclus ont des [attributions et licences distinctes](https://github.com/xShadyy/H5Reclaim/blob/main/corpus/README.md).

<sub>Créé par Tymoteusz Netter.</sub>
