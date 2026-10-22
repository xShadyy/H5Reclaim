<p align="center">
  <img src="../assets/h5reclaim-banner.png" alt="H5Reclaim: استعادة بيانات HDF5 العلمية" width="760">
</p>

<p align="center">
  <a href="../pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="../LICENSE"><img alt="الترخيص: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json"><img alt="اختبار مضبوط: استعادة مفيدة بنسبة 95.4%" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <strong>اللغات:</strong>
  <a href="../README.md" lang="en">English</a> · <a href="README.pl.md" lang="pl">Polski</a> · <a href="README.de.md" lang="de">Deutsch</a> · <a href="README.fr.md" lang="fr">Français</a> · <a href="README.es.md" lang="es">Español</a> · <a href="README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="README.ja.md" lang="ja">日本語</a> · <a href="README.ko.md" lang="ko">한국어</a> · <a href="README.ru.md" lang="ru">Русский</a> · <a href="README.ar.md" lang="ar">العربية</a>
</p>

يستعيد H5Reclaim البيانات العلمية من ملفات HDF5 التالفة. وينقل القياسات التي بقيت سليمة إلى ملف جديد قابل للاستخدام بعد انقطاع الكتابة، أو تلف الفهارس والبيانات الوصفية، أو اقتطاع الملف. وHDF5 هو تنسيق تستخدمه أجهزة وتطبيقات علمية كثيرة لتخزين المصفوفات والقياسات والبيانات الوصفية المرتبطة بها.

**بأمر واحد، يكتشف مجموعات البيانات، ويختار طرائق الاستعادة، وينشئ ملف HDF5 جديدًا مع تقرير JSON واضح.** يبقى الملف الأصلي دون تغيير. ويحتفظ H5Reclaim بالقياسات القابلة للقراءة من مجموعات البيانات المتضررة جزئيًا، ثم يواصل استعادة بقية الملف.

حقق الإصدار 1.0.0 **استعادة مفيدة بنسبة 95.4% في 109 تجارب مضبوطة على ملفات تالفة**: 77 حالة استعادة كاملة ودقيقة، و27 حالة استعادة جزئية، دون أي قيم خاطئة مقبولة في هذه المجموعة التجريبية. تقيس هذه التجارب المُولَّدة أنواع التلف المحددة فيها، ولا تمثل نسبة نجاح متوقعة لملفات أخرى. [اطّلع على النتائج](https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json).

<a id="quick-start"></a>
## البدء السريع

تحتاج إلى Python 3.10 أو إصدار أحدث. استنسخ هذا المستودع، أو نزّل ملف ZIP الخاص به من GitHub وفك ضغطه:

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
```

ثبّت الأداة من جذر المستودع واستعد أحد الملفات:

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

## فهم النتائج

يعرض ملخص الطرفية ما إذا كانت الاستعادة كاملة أو جزئية، ويذكر مواقع ملفات الإخراج. ويحدد تقرير JSON مجموعات البيانات المستعادة، وطرائق الاستعادة، والمواضع التي لم تُستعد، والبيانات الوصفية التي أُعيدت.

عندما ينشر مسار الاستعادة **خريطة حالة**، فإنها تبيّن المواضع التي تحتوي على قياسات مقبولة. استخدم الخريطة المذكورة في التقرير لاختيار القيم التي ستدخل في التحليل؛ فقد تُعرض قيمة ملء مثل الصفر في المواضع غير المعروفة. بعض مجموعات البيانات المتصلة والسليمة، ومجموعات البيانات الفارغة من النوع null، لا تملك خريطة قابلة للفحص؛ لذلك راجع التقرير الخاص بمسار استعادتها قبل التحليل. وتضيف النسخ المرجعية السابقة وحزم الحماية إمكانية المقارنة مع لقطة محفوظة من وقت أسبق.

## الترخيص

تتوفر الشفرة المصدرية بموجب [رخصة Apache 2.0](../LICENSE). وللملفات العلمية المضمّنة [نسب إلى مصادرها وتراخيص منفصلة](https://github.com/xShadyy/H5Reclaim/blob/main/corpus/README.md).

<sub>أنشأه Tymoteusz Netter.</sub>
