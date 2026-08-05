(function (root) {
  "use strict";

  const CODE_RESOLVER_PATH = "/api/public/tt-code/resolve";
  const FEATURED_PATH = "/api/public/tt-drama/featured-by-language";
  const TARGET_ORIGIN = "https://www.dramawavew2a.com";
  const TARGET_PATH = "/ads/101/2250/view";
  const SEARCH_SOURCE = "Search";
  const FEATURED_SOURCE = "Featured";
  const REQUEST_TIMEOUT_MS = 8000;
  const FEATURED_TIMEOUT_MS = 2000;
  const FEATURED_MAX_STALE_MS = 72 * 60 * 60 * 1000;
  const FEATURED_MAX_FUTURE_SKEW_MS = 24 * 60 * 60 * 1000;
  const DRAG_THRESHOLD_PX = 7;
  const CODE_PATTERN = /^[A-Z0-9]{4}$/;
  const CONTENT_ID_PATTERN = /^[A-Za-z0-9_-]{10,32}$/;
  const SAFE_TOKEN_PATTERN = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/;
  const LANGUAGE_TAG_PATTERN = /^[a-z0-9]{1,8}(?:-[a-z0-9]{1,8}){0,3}$/;
  const FEATURED_BUCKET_LANGUAGE_PATTERN = /^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$/;
  const MAX_FEATURED_LANGUAGE_BUCKETS = 32;
  const TARGET_PARAM_KEYS = Object.freeze([
    "af_dp",
    "c",
    "af_adset",
    "af_adset_id",
    "af_ad",
    "af_ad_id",
    "af_channel",
    "af_c_id"
  ]);
  const TARGET_PARAM_KEY_SET = new Set(TARGET_PARAM_KEYS);
  const FEATURED_COVER_HOSTS = new Set([
    "ads-cdn.yingliang.tech",
    "cdn.usrgrow.com",
    "static.mydramawave.com",
    "static-v1.mydramawave.com",
    "static-v2.mydramawave.com"
  ]);

  const EN_COPY = Object.freeze({
    documentTitle: "Enter the code and keep watching | DramaWave",
    brandPill: "Code search",
    eyebrow: "Continue the story",
    titleLead: "Enter the code and ",
    titleAccent: "keep watching",
    searchAria: "Search by short code or DramaWave Content ID",
    searchLabel: "Short code or Content ID",
    exactMatch: "Exact match",
    placeholder: "e.g. A7K2 or Content ID",
    findAria: "Find story",
    helperInitial: "Codes use four letters or numbers. Content IDs use the complete 10–32 character value.",
    matchConfirmed: "Match confirmed",
    continueText: "Open matching story",
    recentTitle: "Recently featured",
    recentNote: "Swipe, drag, or use the arrows",
    controlsAria: "Featured story controls",
    previousAria: "Previous featured stories",
    nextAria: "Next featured stories",
    storiesAria: "Featured stories",
    footer: "Story and destination are verified before DramaWave opens",
    yesterdayTop: "Yesterday's top stories",
    featuredTitle: "Featured stories",
    featuredTimeout: "Featured stories took too long",
    openStoryAria: "Open {title} in DramaWave",
    coverAlt: "{title} cover",
    episodes: "{count} episodes",
    descriptionUnavailable: "Story description is not available yet.",
    matchMessage: "Match confirmed. Tap below to continue in DramaWave.",
    inputInvalid: "Enter a four-character code or the complete 10–32 character Content ID.",
    finding: "Finding and verifying your story…",
    notFound: "We couldn’t find that code or Content ID. Check it and try again.",
    tooMany: "Too many searches. Wait a moment and try again.",
    searchTimeout: "Story search took too long. Please try again.",
    searchUnavailable: "Story search is temporarily unavailable. Please try again.",
    helperIdle: "Enter a four-character code or the complete Content ID.",
    opening: "Opening DramaWave",
    checking: "Checking story…",
    storyUnavailable: "Story unavailable",
    storyCheckTimeout: "Story check took too long",
    retry: "Please try again",
    featuredStory: "Featured story"
  });

  const COPY = Object.freeze({
    en: EN_COPY,
    es: Object.freeze({
      documentTitle: "Ingresa el código y sigue viendo | DramaWave",
      brandPill: "Buscar código",
      eyebrow: "Continúa la historia",
      titleLead: "Ingresa el código y ",
      titleAccent: "sigue viendo",
      searchAria: "Buscar por código corto o Content ID de DramaWave",
      searchLabel: "Código corto o Content ID",
      exactMatch: "Coincidencia exacta",
      placeholder: "p. ej., A7K2 o Content ID",
      findAria: "Buscar historia",
      helperInitial: "Los códigos tienen cuatro letras o números. Usa el valor completo de 10 a 32 caracteres para el Content ID.",
      matchConfirmed: "Coincidencia confirmada",
      continueText: "Abrir la historia encontrada",
      recentTitle: "Destacadas recientemente",
      recentNote: "Desliza, arrastra o usa las flechas",
      controlsAria: "Controles de historias destacadas",
      previousAria: "Historias destacadas anteriores",
      nextAria: "Siguientes historias destacadas",
      storiesAria: "Historias destacadas",
      footer: "Verificamos la historia y el destino antes de abrir DramaWave",
      yesterdayTop: "Las historias más vistas de ayer",
      featuredTitle: "Historias destacadas",
      featuredTimeout: "Las historias destacadas tardaron demasiado",
      openStoryAria: "Abrir {title} en DramaWave",
      coverAlt: "Portada de {title}",
      episodes: "{count} episodios",
      descriptionUnavailable: "La descripción aún no está disponible.",
      matchMessage: "Coincidencia confirmada. Toca abajo para continuar en DramaWave.",
      inputInvalid: "Ingresa un código de cuatro caracteres o el Content ID completo de 10 a 32 caracteres.",
      finding: "Buscando y verificando tu historia…",
      notFound: "No encontramos ese código o Content ID. Revísalo e inténtalo de nuevo.",
      tooMany: "Demasiadas búsquedas. Espera un momento e inténtalo de nuevo.",
      searchTimeout: "La búsqueda tardó demasiado. Inténtalo de nuevo.",
      searchUnavailable: "La búsqueda no está disponible temporalmente. Inténtalo de nuevo.",
      helperIdle: "Ingresa un código de cuatro caracteres o el Content ID completo.",
      opening: "Abriendo DramaWave",
      checking: "Verificando la historia…",
      storyUnavailable: "Historia no disponible",
      storyCheckTimeout: "La verificación tardó demasiado",
      retry: "Inténtalo de nuevo",
      featuredStory: "Historia destacada"
    }),
    pt: Object.freeze({
      documentTitle: "Insira o código e continue assistindo | DramaWave",
      brandPill: "Pesquisa por código",
      eyebrow: "Continue a história",
      titleLead: "Insira o código e ",
      titleAccent: "continue assistindo",
      searchAria: "Pesquisar por código curto ou Content ID do DramaWave",
      searchLabel: "Código curto ou Content ID",
      exactMatch: "Correspondência exata",
      placeholder: "Ex.: A7K2 ou Content ID",
      findAria: "Encontrar história",
      helperInitial: "Os códigos têm quatro letras ou números. Use o valor completo de 10 a 32 caracteres do Content ID.",
      matchConfirmed: "Correspondência confirmada",
      continueText: "Abrir história encontrada",
      recentTitle: "Destaques recentes",
      recentNote: "Deslize, arraste ou use as setas",
      controlsAria: "Controles das histórias em destaque",
      previousAria: "Histórias em destaque anteriores",
      nextAria: "Próximas histórias em destaque",
      storiesAria: "Histórias em destaque",
      footer: "A história e o destino são verificados antes de abrir o DramaWave",
      yesterdayTop: "Histórias mais vistas de ontem",
      featuredTitle: "Histórias em destaque",
      featuredTimeout: "As histórias em destaque demoraram demais",
      openStoryAria: "Abrir {title} no DramaWave",
      coverAlt: "Capa de {title}",
      episodes: "{count} episódios",
      descriptionUnavailable: "A descrição ainda não está disponível.",
      matchMessage: "Correspondência confirmada. Toque abaixo para continuar no DramaWave.",
      inputInvalid: "Insira um código de quatro caracteres ou o Content ID completo de 10 a 32 caracteres.",
      finding: "Buscando e verificando sua história…",
      notFound: "Não encontramos esse código ou Content ID. Confira e tente novamente.",
      tooMany: "Muitas buscas. Aguarde um momento e tente novamente.",
      searchTimeout: "A busca demorou demais. Tente novamente.",
      searchUnavailable: "A busca está temporariamente indisponível. Tente novamente.",
      helperIdle: "Insira um código de quatro caracteres ou o Content ID completo.",
      opening: "Abrindo o DramaWave",
      checking: "Verificando a história…",
      storyUnavailable: "História indisponível",
      storyCheckTimeout: "A verificação demorou demais",
      retry: "Tente novamente",
      featuredStory: "História em destaque"
    }),
    th: Object.freeze({
      documentTitle: "กรอกรหัสแล้วดูต่อ | DramaWave",
      brandPill: "ค้นหาด้วยรหัส",
      eyebrow: "ดูเรื่องราวต่อ",
      titleLead: "กรอกรหัสแล้ว",
      titleAccent: "ดูต่อ",
      searchAria: "ค้นหาด้วยรหัสสั้นหรือ Content ID ของ DramaWave",
      searchLabel: "รหัสสั้นหรือ Content ID",
      exactMatch: "ตรงกันทุกตัว",
      placeholder: "เช่น A7K2 หรือ Content ID",
      findAria: "ค้นหาเรื่อง",
      helperInitial: "รหัสใช้ตัวอักษรหรือตัวเลข 4 ตัว ส่วน Content ID ให้กรอกค่าครบ 10–32 ตัวอักษร",
      matchConfirmed: "พบเรื่องที่ตรงกัน",
      continueText: "เปิดเรื่องที่ตรงกัน",
      recentTitle: "เรื่องเด่นล่าสุด",
      recentNote: "ปัด ลาก หรือใช้ลูกศร",
      controlsAria: "ตัวควบคุมเรื่องเด่น",
      previousAria: "เรื่องเด่นก่อนหน้า",
      nextAria: "เรื่องเด่นถัดไป",
      storiesAria: "เรื่องเด่น",
      footer: "ระบบจะตรวจสอบเรื่องและปลายทางก่อนเปิด DramaWave",
      yesterdayTop: "เรื่องยอดนิยมเมื่อวาน",
      featuredTitle: "เรื่องเด่น",
      featuredTimeout: "โหลดเรื่องเด่นนานเกินไป",
      openStoryAria: "เปิด {title} ใน DramaWave",
      coverAlt: "ภาพปกของ {title}",
      episodes: "{count} ตอน",
      descriptionUnavailable: "ยังไม่มีคำอธิบายเรื่อง",
      matchMessage: "พบเรื่องที่ตรงกัน แตะด้านล่างเพื่อดูต่อใน DramaWave",
      inputInvalid: "กรอกรหัส 4 ตัว หรือ Content ID แบบครบ 10–32 ตัวอักษร",
      finding: "กำลังค้นหาและตรวจสอบเรื่องของคุณ…",
      notFound: "ไม่พบรหัสหรือ Content ID นี้ โปรดตรวจสอบแล้วลองอีกครั้ง",
      tooMany: "ค้นหาบ่อยเกินไป รอสักครู่แล้วลองอีกครั้ง",
      searchTimeout: "การค้นหาใช้เวลานานเกินไป โปรดลองอีกครั้ง",
      searchUnavailable: "การค้นหาไม่พร้อมใช้งานชั่วคราว โปรดลองอีกครั้ง",
      helperIdle: "กรอกรหัส 4 ตัว หรือ Content ID แบบครบถ้วน",
      opening: "กำลังเปิด DramaWave",
      checking: "กำลังตรวจสอบเรื่อง…",
      storyUnavailable: "เรื่องนี้ไม่พร้อมใช้งาน",
      storyCheckTimeout: "การตรวจสอบใช้เวลานานเกินไป",
      retry: "โปรดลองอีกครั้ง",
      featuredStory: "เรื่องเด่น"
    }),
    id: Object.freeze({
      documentTitle: "Masukkan kode dan lanjutkan menonton | DramaWave",
      brandPill: "Pencarian kode",
      eyebrow: "Lanjutkan ceritanya",
      titleLead: "Masukkan kode dan ",
      titleAccent: "lanjutkan menonton",
      searchAria: "Cari dengan kode pendek atau Content ID DramaWave",
      searchLabel: "Kode pendek atau Content ID",
      exactMatch: "Cocok persis",
      placeholder: "mis. A7K2 atau Content ID",
      findAria: "Cari cerita",
      helperInitial: "Kode terdiri dari empat huruf atau angka. Gunakan nilai lengkap 10–32 karakter untuk Content ID.",
      matchConfirmed: "Kecocokan ditemukan",
      continueText: "Buka cerita yang cocok",
      recentTitle: "Baru ditampilkan",
      recentNote: "Geser, seret, atau gunakan panah",
      controlsAria: "Kontrol cerita unggulan",
      previousAria: "Cerita unggulan sebelumnya",
      nextAria: "Cerita unggulan berikutnya",
      storiesAria: "Cerita unggulan",
      footer: "Cerita dan tujuan diperiksa sebelum DramaWave dibuka",
      yesterdayTop: "Cerita teratas kemarin",
      featuredTitle: "Cerita unggulan",
      featuredTimeout: "Cerita unggulan terlalu lama dimuat",
      openStoryAria: "Buka {title} di DramaWave",
      coverAlt: "Sampul {title}",
      episodes: "{count} episode",
      descriptionUnavailable: "Deskripsi cerita belum tersedia.",
      matchMessage: "Kecocokan ditemukan. Ketuk di bawah untuk melanjutkan di DramaWave.",
      inputInvalid: "Masukkan kode empat karakter atau Content ID lengkap 10–32 karakter.",
      finding: "Mencari dan memeriksa cerita Anda…",
      notFound: "Kode atau Content ID itu tidak ditemukan. Periksa dan coba lagi.",
      tooMany: "Terlalu banyak pencarian. Tunggu sebentar lalu coba lagi.",
      searchTimeout: "Pencarian terlalu lama. Coba lagi.",
      searchUnavailable: "Pencarian sementara tidak tersedia. Coba lagi.",
      helperIdle: "Masukkan kode empat karakter atau Content ID lengkap.",
      opening: "Membuka DramaWave",
      checking: "Memeriksa cerita…",
      storyUnavailable: "Cerita tidak tersedia",
      storyCheckTimeout: "Pemeriksaan terlalu lama",
      retry: "Coba lagi",
      featuredStory: "Cerita unggulan"
    }),
    ja: Object.freeze({
      documentTitle: "コードを入力して続きを見る | DramaWave",
      brandPill: "コード検索",
      eyebrow: "物語の続きを見る",
      titleLead: "コードを入力して",
      titleAccent: "続きを見る",
      searchAria: "短縮コードまたはDramaWaveのContent IDで検索",
      searchLabel: "短縮コードまたはContent ID",
      exactMatch: "完全一致",
      placeholder: "例：A7K2 または Content ID",
      findAria: "作品を検索",
      helperInitial: "コードは英数字4文字です。Content IDは10～32文字の完全な値を入力してください。",
      matchConfirmed: "一致を確認しました",
      continueText: "一致した作品を開く",
      recentTitle: "最近の注目作品",
      recentNote: "スワイプ、ドラッグ、または矢印で移動",
      controlsAria: "注目作品の操作",
      previousAria: "前の注目作品",
      nextAria: "次の注目作品",
      storiesAria: "注目作品",
      footer: "DramaWaveを開く前に作品とリンク先を確認します",
      yesterdayTop: "昨日の人気作品",
      featuredTitle: "注目作品",
      featuredTimeout: "注目作品の読み込みに時間がかかっています",
      openStoryAria: "{title}をDramaWaveで開く",
      coverAlt: "{title}のカバー",
      episodes: "{count}話",
      descriptionUnavailable: "作品の説明はまだありません。",
      matchMessage: "一致を確認しました。下をタップしてDramaWaveで続きをご覧ください。",
      inputInvalid: "4文字のコードまたは10～32文字の完全なContent IDを入力してください。",
      finding: "作品を検索して確認しています…",
      notFound: "そのコードまたはContent IDは見つかりませんでした。確認してもう一度お試しください。",
      tooMany: "検索回数が多すぎます。少し待ってからもう一度お試しください。",
      searchTimeout: "検索に時間がかかりすぎました。もう一度お試しください。",
      searchUnavailable: "検索は一時的に利用できません。もう一度お試しください。",
      helperIdle: "4文字のコードまたは完全なContent IDを入力してください。",
      opening: "DramaWaveを開いています",
      checking: "作品を確認しています…",
      storyUnavailable: "作品を利用できません",
      storyCheckTimeout: "作品の確認に時間がかかりすぎました",
      retry: "もう一度お試しください",
      featuredStory: "注目作品"
    }),
    tr: Object.freeze({
      documentTitle: "Kodu girin ve izlemeye devam edin | DramaWave",
      brandPill: "Kod arama",
      eyebrow: "Hikâyeye devam edin",
      titleLead: "Kodu girin ve ",
      titleAccent: "izlemeye devam edin",
      searchAria: "Kısa kod veya DramaWave Content ID ile ara",
      searchLabel: "Kısa kod veya Content ID",
      exactMatch: "Tam eşleşme",
      placeholder: "örn. A7K2 veya Content ID",
      findAria: "Hikâyeyi bul",
      helperInitial: "Kodlar dört harf veya rakamdan oluşur. Content ID için 10–32 karakterlik değerin tamamını girin.",
      matchConfirmed: "Eşleşme doğrulandı",
      continueText: "Eşleşen hikâyeyi aç",
      recentTitle: "Son öne çıkanlar",
      recentNote: "Kaydırın, sürükleyin veya okları kullanın",
      controlsAria: "Öne çıkan hikâye kontrolleri",
      previousAria: "Önceki öne çıkan hikâyeler",
      nextAria: "Sonraki öne çıkan hikâyeler",
      storiesAria: "Öne çıkan hikâyeler",
      footer: "DramaWave açılmadan önce hikâye ve hedef doğrulanır",
      yesterdayTop: "Dünün en popüler hikâyeleri",
      featuredTitle: "Öne çıkan hikâyeler",
      featuredTimeout: "Öne çıkan hikâyeler çok geç yüklendi",
      openStoryAria: "{title} hikâyesini DramaWave'de aç",
      coverAlt: "{title} kapak görseli",
      episodes: "{count} bölüm",
      descriptionUnavailable: "Hikâye açıklaması henüz mevcut değil.",
      matchMessage: "Eşleşme doğrulandı. DramaWave'de devam etmek için aşağıya dokunun.",
      inputInvalid: "Dört karakterli bir kod veya 10–32 karakterlik Content ID'nin tamamını girin.",
      finding: "Hikâyeniz aranıyor ve doğrulanıyor…",
      notFound: "Bu kod veya Content ID bulunamadı. Kontrol edip tekrar deneyin.",
      tooMany: "Çok fazla arama yaptınız. Biraz bekleyip tekrar deneyin.",
      searchTimeout: "Hikâye araması çok uzun sürdü. Tekrar deneyin.",
      searchUnavailable: "Hikâye araması geçici olarak kullanılamıyor. Tekrar deneyin.",
      helperIdle: "Dört karakterli bir kod veya Content ID'nin tamamını girin.",
      opening: "DramaWave açılıyor",
      checking: "Hikâye kontrol ediliyor…",
      storyUnavailable: "Hikâye kullanılamıyor",
      storyCheckTimeout: "Hikâye kontrolü çok uzun sürdü",
      retry: "Tekrar deneyin",
      featuredStory: "Öne çıkan hikâye"
    }),
    fr: Object.freeze({
      documentTitle: "Entrez le code et continuez à regarder | DramaWave",
      brandPill: "Recherche par code",
      eyebrow: "Continuez l’histoire",
      titleLead: "Entrez le code et ",
      titleAccent: "continuez à regarder",
      searchAria: "Rechercher avec un code court ou un Content ID DramaWave",
      searchLabel: "Code court ou Content ID",
      exactMatch: "Correspondance exacte",
      placeholder: "ex. A7K2 ou Content ID",
      findAria: "Trouver l’histoire",
      helperInitial: "Les codes comportent quatre lettres ou chiffres. Pour le Content ID, saisissez les 10 à 32 caractères.",
      matchConfirmed: "Correspondance confirmée",
      continueText: "Ouvrir l’histoire correspondante",
      recentTitle: "Récemment à l’affiche",
      recentNote: "Balayez, faites glisser ou utilisez les flèches",
      controlsAria: "Commandes des histoires à l’affiche",
      previousAria: "Histoires précédentes",
      nextAria: "Histoires suivantes",
      storiesAria: "Histoires à l’affiche",
      footer: "L’histoire et la destination sont vérifiées avant l’ouverture de DramaWave",
      yesterdayTop: "Les meilleures histoires d’hier",
      featuredTitle: "Histoires à l’affiche",
      featuredTimeout: "Le chargement des histoires a pris trop de temps",
      openStoryAria: "Ouvrir {title} dans DramaWave",
      coverAlt: "Affiche de {title}",
      episodes: "{count} épisodes",
      descriptionUnavailable: "La description n’est pas encore disponible.",
      matchMessage: "Correspondance confirmée. Touchez ci-dessous pour continuer dans DramaWave.",
      inputInvalid: "Saisissez un code de quatre caractères ou le Content ID complet de 10 à 32 caractères.",
      finding: "Recherche et vérification de votre histoire…",
      notFound: "Ce code ou Content ID est introuvable. Vérifiez-le et réessayez.",
      tooMany: "Trop de recherches. Patientez un instant et réessayez.",
      searchTimeout: "La recherche a pris trop de temps. Réessayez.",
      searchUnavailable: "La recherche est temporairement indisponible. Réessayez.",
      helperIdle: "Saisissez un code de quatre caractères ou le Content ID complet.",
      opening: "Ouverture de DramaWave",
      checking: "Vérification de l’histoire…",
      storyUnavailable: "Histoire indisponible",
      storyCheckTimeout: "La vérification a pris trop de temps",
      retry: "Réessayez",
      featuredStory: "Histoire à l’affiche"
    }),
    ar: Object.freeze({
      documentTitle: "أدخل الرمز وتابع المشاهدة | DramaWave",
      brandPill: "البحث بالرمز",
      eyebrow: "تابع القصة",
      titleLead: "أدخل الرمز و",
      titleAccent: "تابع المشاهدة",
      searchAria: "البحث برمز قصير أو Content ID في DramaWave",
      searchLabel: "الرمز القصير أو Content ID",
      exactMatch: "تطابق تام",
      placeholder: "مثال: A7K2 أو Content ID",
      findAria: "العثور على القصة",
      helperInitial: "يتكون الرمز من أربعة أحرف أو أرقام. أدخل قيمة Content ID كاملة من 10 إلى 32 حرفًا.",
      matchConfirmed: "تم تأكيد التطابق",
      continueText: "فتح القصة المطابقة",
      recentTitle: "المضاف حديثًا",
      recentNote: "اسحب أو مرّر أو استخدم الأسهم",
      controlsAria: "عناصر التحكم في القصص المميزة",
      previousAria: "القصص المميزة السابقة",
      nextAria: "القصص المميزة التالية",
      storiesAria: "قصص مميزة",
      footer: "يتم التحقق من القصة والوجهة قبل فتح DramaWave",
      yesterdayTop: "أفضل قصص الأمس",
      featuredTitle: "قصص مميزة",
      featuredTimeout: "استغرق تحميل القصص المميزة وقتًا طويلًا",
      openStoryAria: "فتح {title} في DramaWave",
      coverAlt: "غلاف {title}",
      episodes: "{count} حلقة",
      descriptionUnavailable: "وصف القصة غير متاح بعد.",
      matchMessage: "تم تأكيد التطابق. اضغط أدناه للمتابعة في DramaWave.",
      inputInvalid: "أدخل رمزًا من أربعة أحرف أو Content ID كاملًا من 10 إلى 32 حرفًا.",
      finding: "جارٍ البحث عن قصتك والتحقق منها…",
      notFound: "تعذر العثور على هذا الرمز أو Content ID. تحقّق منه وحاول مجددًا.",
      tooMany: "عمليات بحث كثيرة جدًا. انتظر قليلًا ثم حاول مجددًا.",
      searchTimeout: "استغرق البحث وقتًا طويلًا. حاول مجددًا.",
      searchUnavailable: "البحث غير متاح مؤقتًا. حاول مجددًا.",
      helperIdle: "أدخل رمزًا من أربعة أحرف أو Content ID كاملًا.",
      opening: "جارٍ فتح DramaWave",
      checking: "جارٍ التحقق من القصة…",
      storyUnavailable: "القصة غير متاحة",
      storyCheckTimeout: "استغرق التحقق وقتًا طويلًا",
      retry: "حاول مجددًا",
      featuredStory: "قصة مميزة"
    }),
    de: Object.freeze({
      documentTitle: "Code eingeben und weiterschauen | DramaWave",
      brandPill: "Code-Suche",
      eyebrow: "Geschichte fortsetzen",
      titleLead: "Code eingeben und ",
      titleAccent: "weiterschauen",
      searchAria: "Mit Kurzcode oder DramaWave Content ID suchen",
      searchLabel: "Kurzcode oder Content ID",
      exactMatch: "Exakte Übereinstimmung",
      placeholder: "z. B. A7K2 oder Content ID",
      findAria: "Geschichte finden",
      helperInitial: "Codes bestehen aus vier Buchstaben oder Ziffern. Geben Sie die vollständige, 10–32 Zeichen lange Content ID ein.",
      matchConfirmed: "Übereinstimmung bestätigt",
      continueText: "Passende Geschichte öffnen",
      recentTitle: "Kürzlich vorgestellt",
      recentNote: "Wischen, ziehen oder Pfeile verwenden",
      controlsAria: "Steuerung für vorgestellte Geschichten",
      previousAria: "Vorherige vorgestellte Geschichten",
      nextAria: "Nächste vorgestellte Geschichten",
      storiesAria: "Vorgestellte Geschichten",
      footer: "Geschichte und Ziel werden vor dem Öffnen von DramaWave geprüft",
      yesterdayTop: "Die Top-Geschichten von gestern",
      featuredTitle: "Vorgestellte Geschichten",
      featuredTimeout: "Das Laden der vorgestellten Geschichten dauerte zu lange",
      openStoryAria: "{title} in DramaWave öffnen",
      coverAlt: "Cover von {title}",
      episodes: "{count} Folgen",
      descriptionUnavailable: "Noch keine Beschreibung verfügbar.",
      matchMessage: "Übereinstimmung bestätigt. Tippen Sie unten, um in DramaWave weiterzuschauen.",
      inputInvalid: "Geben Sie einen vierstelligen Code oder die vollständige 10–32 Zeichen lange Content ID ein.",
      finding: "Ihre Geschichte wird gesucht und geprüft…",
      notFound: "Dieser Code oder diese Content ID wurde nicht gefunden. Prüfen Sie die Eingabe und versuchen Sie es erneut.",
      tooMany: "Zu viele Suchanfragen. Warten Sie kurz und versuchen Sie es erneut.",
      searchTimeout: "Die Suche dauerte zu lange. Versuchen Sie es erneut.",
      searchUnavailable: "Die Suche ist vorübergehend nicht verfügbar. Versuchen Sie es erneut.",
      helperIdle: "Geben Sie einen vierstelligen Code oder die vollständige Content ID ein.",
      opening: "DramaWave wird geöffnet",
      checking: "Geschichte wird geprüft…",
      storyUnavailable: "Geschichte nicht verfügbar",
      storyCheckTimeout: "Die Prüfung dauerte zu lange",
      retry: "Bitte erneut versuchen",
      featuredStory: "Vorgestellte Geschichte"
    }),
    pl: Object.freeze({
      documentTitle: "Wpisz kod i oglądaj dalej | DramaWave",
      brandPill: "Wyszukiwanie kodu",
      eyebrow: "Kontynuuj historię",
      titleLead: "Wpisz kod i ",
      titleAccent: "oglądaj dalej",
      searchAria: "Wyszukaj za pomocą krótkiego kodu lub DramaWave Content ID",
      searchLabel: "Krótki kod lub Content ID",
      exactMatch: "Dokładne dopasowanie",
      placeholder: "np. A7K2 lub Content ID",
      findAria: "Znajdź historię",
      helperInitial: "Kod składa się z czterech liter lub cyfr. Wpisz pełny Content ID o długości 10–32 znaków.",
      matchConfirmed: "Dopasowanie potwierdzone",
      continueText: "Otwórz pasującą historię",
      recentTitle: "Ostatnio polecane",
      recentNote: "Przesuń, przeciągnij lub użyj strzałek",
      controlsAria: "Sterowanie polecanymi historiami",
      previousAria: "Poprzednie polecane historie",
      nextAria: "Następne polecane historie",
      storiesAria: "Polecane historie",
      footer: "Historia i cel są sprawdzane przed otwarciem DramaWave",
      yesterdayTop: "Najpopularniejsze historie wczoraj",
      featuredTitle: "Polecane historie",
      featuredTimeout: "Ładowanie polecanych historii trwało zbyt długo",
      openStoryAria: "Otwórz {title} w DramaWave",
      coverAlt: "Okładka {title}",
      episodes: "{count} odc.",
      descriptionUnavailable: "Opis historii nie jest jeszcze dostępny.",
      matchMessage: "Dopasowanie potwierdzone. Dotknij poniżej, aby kontynuować w DramaWave.",
      inputInvalid: "Wpisz czteroznakowy kod lub pełny Content ID o długości 10–32 znaków.",
      finding: "Wyszukiwanie i sprawdzanie historii…",
      notFound: "Nie znaleziono tego kodu lub Content ID. Sprawdź dane i spróbuj ponownie.",
      tooMany: "Zbyt wiele wyszukiwań. Poczekaj chwilę i spróbuj ponownie.",
      searchTimeout: "Wyszukiwanie trwało zbyt długo. Spróbuj ponownie.",
      searchUnavailable: "Wyszukiwanie jest chwilowo niedostępne. Spróbuj ponownie.",
      helperIdle: "Wpisz czteroznakowy kod lub pełny Content ID.",
      opening: "Otwieranie DramaWave",
      checking: "Sprawdzanie historii…",
      storyUnavailable: "Historia niedostępna",
      storyCheckTimeout: "Sprawdzanie trwało zbyt długo",
      retry: "Spróbuj ponownie",
      featuredStory: "Polecana historia"
    }),
    ko: Object.freeze({
      documentTitle: "코드를 입력하고 계속 시청하세요 | DramaWave",
      brandPill: "코드 검색",
      eyebrow: "이야기 이어 보기",
      titleLead: "코드를 입력하고 ",
      titleAccent: "계속 시청하세요",
      searchAria: "짧은 코드 또는 DramaWave Content ID로 검색",
      searchLabel: "짧은 코드 또는 Content ID",
      exactMatch: "정확히 일치",
      placeholder: "예: A7K2 또는 Content ID",
      findAria: "작품 찾기",
      helperInitial: "코드는 영문 또는 숫자 4자리입니다. Content ID는 10~32자의 전체 값을 입력하세요.",
      matchConfirmed: "일치 항목 확인됨",
      continueText: "일치하는 작품 열기",
      recentTitle: "최근 추천작",
      recentNote: "밀거나 드래그하거나 화살표를 사용하세요",
      controlsAria: "추천 작품 컨트롤",
      previousAria: "이전 추천 작품",
      nextAria: "다음 추천 작품",
      storiesAria: "추천 작품",
      footer: "DramaWave를 열기 전에 작품과 이동 경로를 확인합니다",
      yesterdayTop: "어제의 인기 작품",
      featuredTitle: "추천 작품",
      featuredTimeout: "추천 작품을 불러오는 데 너무 오래 걸렸습니다",
      openStoryAria: "DramaWave에서 {title} 열기",
      coverAlt: "{title} 표지",
      episodes: "{count}화",
      descriptionUnavailable: "작품 설명이 아직 없습니다.",
      matchMessage: "일치 항목을 확인했습니다. 아래를 눌러 DramaWave에서 계속 시청하세요.",
      inputInvalid: "4자리 코드 또는 10~32자의 전체 Content ID를 입력하세요.",
      finding: "작품을 찾고 확인하는 중…",
      notFound: "해당 코드 또는 Content ID를 찾을 수 없습니다. 확인 후 다시 시도하세요.",
      tooMany: "검색 횟수가 너무 많습니다. 잠시 후 다시 시도하세요.",
      searchTimeout: "검색 시간이 너무 오래 걸렸습니다. 다시 시도하세요.",
      searchUnavailable: "검색을 일시적으로 사용할 수 없습니다. 다시 시도하세요.",
      helperIdle: "4자리 코드 또는 전체 Content ID를 입력하세요.",
      opening: "DramaWave 여는 중",
      checking: "작품 확인 중…",
      storyUnavailable: "작품을 이용할 수 없습니다",
      storyCheckTimeout: "작품 확인 시간이 너무 오래 걸렸습니다",
      retry: "다시 시도하세요",
      featuredStory: "추천 작품"
    }),
    ru: Object.freeze({
      documentTitle: "Введите код и продолжайте смотреть | DramaWave",
      brandPill: "Поиск по коду",
      eyebrow: "Продолжите историю",
      titleLead: "Введите код и ",
      titleAccent: "продолжайте смотреть",
      searchAria: "Поиск по короткому коду или DramaWave Content ID",
      searchLabel: "Короткий код или Content ID",
      exactMatch: "Точное совпадение",
      placeholder: "например, A7K2 или Content ID",
      findAria: "Найти историю",
      helperInitial: "Код состоит из четырёх букв или цифр. Введите полный Content ID длиной 10–32 символа.",
      matchConfirmed: "Совпадение подтверждено",
      continueText: "Открыть найденную историю",
      recentTitle: "Недавно в подборке",
      recentNote: "Проведите, перетащите или используйте стрелки",
      controlsAria: "Управление подборкой историй",
      previousAria: "Предыдущие истории",
      nextAria: "Следующие истории",
      storiesAria: "Избранные истории",
      footer: "История и ссылка проверяются до открытия DramaWave",
      yesterdayTop: "Лучшие истории вчера",
      featuredTitle: "Избранные истории",
      featuredTimeout: "Загрузка подборки заняла слишком много времени",
      openStoryAria: "Открыть «{title}» в DramaWave",
      coverAlt: "Обложка «{title}»",
      episodes: "{count} эп.",
      descriptionUnavailable: "Описание истории пока недоступно.",
      matchMessage: "Совпадение подтверждено. Нажмите ниже, чтобы продолжить в DramaWave.",
      inputInvalid: "Введите код из четырёх символов или полный Content ID длиной 10–32 символа.",
      finding: "Ищем и проверяем вашу историю…",
      notFound: "Не удалось найти этот код или Content ID. Проверьте и повторите попытку.",
      tooMany: "Слишком много запросов. Немного подождите и повторите попытку.",
      searchTimeout: "Поиск занял слишком много времени. Повторите попытку.",
      searchUnavailable: "Поиск временно недоступен. Повторите попытку.",
      helperIdle: "Введите код из четырёх символов или полный Content ID.",
      opening: "Открываем DramaWave",
      checking: "Проверяем историю…",
      storyUnavailable: "История недоступна",
      storyCheckTimeout: "Проверка заняла слишком много времени",
      retry: "Повторите попытку",
      featuredStory: "Избранная история"
    }),
    it: Object.freeze({
      documentTitle: "Inserisci il codice e continua a guardare | DramaWave",
      brandPill: "Ricerca codice",
      eyebrow: "Continua la storia",
      titleLead: "Inserisci il codice e ",
      titleAccent: "continua a guardare",
      searchAria: "Cerca con un codice breve o un Content ID DramaWave",
      searchLabel: "Codice breve o Content ID",
      exactMatch: "Corrispondenza esatta",
      placeholder: "es. A7K2 o Content ID",
      findAria: "Trova la storia",
      helperInitial: "I codici hanno quattro lettere o numeri. Inserisci il Content ID completo di 10–32 caratteri.",
      matchConfirmed: "Corrispondenza confermata",
      continueText: "Apri la storia corrispondente",
      recentTitle: "In evidenza di recente",
      recentNote: "Scorri, trascina o usa le frecce",
      controlsAria: "Comandi delle storie in evidenza",
      previousAria: "Storie in evidenza precedenti",
      nextAria: "Storie in evidenza successive",
      storiesAria: "Storie in evidenza",
      footer: "La storia e la destinazione vengono verificate prima di aprire DramaWave",
      yesterdayTop: "Le storie più viste di ieri",
      featuredTitle: "Storie in evidenza",
      featuredTimeout: "Il caricamento delle storie ha richiesto troppo tempo",
      openStoryAria: "Apri {title} in DramaWave",
      coverAlt: "Copertina di {title}",
      episodes: "{count} episodi",
      descriptionUnavailable: "La descrizione non è ancora disponibile.",
      matchMessage: "Corrispondenza confermata. Tocca qui sotto per continuare in DramaWave.",
      inputInvalid: "Inserisci un codice di quattro caratteri o il Content ID completo di 10–32 caratteri.",
      finding: "Ricerca e verifica della tua storia…",
      notFound: "Codice o Content ID non trovato. Controlla e riprova.",
      tooMany: "Troppe ricerche. Attendi un momento e riprova.",
      searchTimeout: "La ricerca ha richiesto troppo tempo. Riprova.",
      searchUnavailable: "La ricerca è temporaneamente non disponibile. Riprova.",
      helperIdle: "Inserisci un codice di quattro caratteri o il Content ID completo.",
      opening: "Apertura di DramaWave",
      checking: "Verifica della storia…",
      storyUnavailable: "Storia non disponibile",
      storyCheckTimeout: "La verifica ha richiesto troppo tempo",
      retry: "Riprova",
      featuredStory: "Storia in evidenza"
    }),
    vi: Object.freeze({
      documentTitle: "Nhập mã và xem tiếp | DramaWave",
      brandPill: "Tìm bằng mã",
      eyebrow: "Xem tiếp câu chuyện",
      titleLead: "Nhập mã và ",
      titleAccent: "xem tiếp",
      searchAria: "Tìm bằng mã ngắn hoặc Content ID DramaWave",
      searchLabel: "Mã ngắn hoặc Content ID",
      exactMatch: "Khớp chính xác",
      placeholder: "ví dụ: A7K2 hoặc Content ID",
      findAria: "Tìm câu chuyện",
      helperInitial: "Mã gồm bốn chữ cái hoặc số. Hãy nhập đầy đủ Content ID dài 10–32 ký tự.",
      matchConfirmed: "Đã xác nhận khớp",
      continueText: "Mở câu chuyện phù hợp",
      recentTitle: "Mới nổi bật",
      recentNote: "Vuốt, kéo hoặc dùng mũi tên",
      controlsAria: "Điều khiển nội dung nổi bật",
      previousAria: "Các câu chuyện nổi bật trước",
      nextAria: "Các câu chuyện nổi bật tiếp theo",
      storiesAria: "Câu chuyện nổi bật",
      footer: "Câu chuyện và đích đến được xác minh trước khi mở DramaWave",
      yesterdayTop: "Câu chuyện nổi bật hôm qua",
      featuredTitle: "Câu chuyện nổi bật",
      featuredTimeout: "Tải câu chuyện nổi bật quá lâu",
      openStoryAria: "Mở {title} trong DramaWave",
      coverAlt: "Ảnh bìa {title}",
      episodes: "{count} tập",
      descriptionUnavailable: "Chưa có mô tả câu chuyện.",
      matchMessage: "Đã xác nhận khớp. Nhấn bên dưới để xem tiếp trong DramaWave.",
      inputInvalid: "Nhập mã gồm bốn ký tự hoặc Content ID đầy đủ dài 10–32 ký tự.",
      finding: "Đang tìm và xác minh câu chuyện của bạn…",
      notFound: "Không tìm thấy mã hoặc Content ID này. Hãy kiểm tra và thử lại.",
      tooMany: "Tìm kiếm quá nhiều. Hãy đợi một chút rồi thử lại.",
      searchTimeout: "Tìm kiếm mất quá nhiều thời gian. Vui lòng thử lại.",
      searchUnavailable: "Tính năng tìm kiếm tạm thời không khả dụng. Vui lòng thử lại.",
      helperIdle: "Nhập mã gồm bốn ký tự hoặc Content ID đầy đủ.",
      opening: "Đang mở DramaWave",
      checking: "Đang kiểm tra câu chuyện…",
      storyUnavailable: "Câu chuyện không khả dụng",
      storyCheckTimeout: "Kiểm tra mất quá nhiều thời gian",
      retry: "Vui lòng thử lại",
      featuredStory: "Câu chuyện nổi bật"
    }),
    ro: Object.freeze({
      documentTitle: "Introdu codul și continuă să vizionezi | DramaWave",
      brandPill: "Căutare după cod",
      eyebrow: "Continuă povestea",
      titleLead: "Introdu codul și ",
      titleAccent: "continuă să vizionezi",
      searchAria: "Caută după cod scurt sau DramaWave Content ID",
      searchLabel: "Cod scurt sau Content ID",
      exactMatch: "Potrivire exactă",
      placeholder: "de ex. A7K2 sau Content ID",
      findAria: "Găsește povestea",
      helperInitial: "Codurile au patru litere sau cifre. Introdu valoarea completă de 10–32 de caractere pentru Content ID.",
      matchConfirmed: "Potrivire confirmată",
      continueText: "Deschide povestea potrivită",
      recentTitle: "Recomandate recent",
      recentNote: "Glisează, trage sau folosește săgețile",
      controlsAria: "Comenzi pentru poveștile recomandate",
      previousAria: "Povești recomandate anterioare",
      nextAria: "Următoarele povești recomandate",
      storiesAria: "Povești recomandate",
      footer: "Povestea și destinația sunt verificate înainte de deschiderea DramaWave",
      yesterdayTop: "Poveștile de top de ieri",
      featuredTitle: "Povești recomandate",
      featuredTimeout: "Încărcarea poveștilor a durat prea mult",
      openStoryAria: "Deschide {title} în DramaWave",
      coverAlt: "Coperta pentru {title}",
      episodes: "{count} episoade",
      descriptionUnavailable: "Descrierea poveștii nu este disponibilă încă.",
      matchMessage: "Potrivire confirmată. Atinge mai jos pentru a continua în DramaWave.",
      inputInvalid: "Introdu un cod de patru caractere sau Content ID complet de 10–32 de caractere.",
      finding: "Căutăm și verificăm povestea…",
      notFound: "Nu am găsit codul sau Content ID. Verifică și încearcă din nou.",
      tooMany: "Prea multe căutări. Așteaptă puțin și încearcă din nou.",
      searchTimeout: "Căutarea a durat prea mult. Încearcă din nou.",
      searchUnavailable: "Căutarea este indisponibilă temporar. Încearcă din nou.",
      helperIdle: "Introdu un cod de patru caractere sau Content ID complet.",
      opening: "Se deschide DramaWave",
      checking: "Se verifică povestea…",
      storyUnavailable: "Poveste indisponibilă",
      storyCheckTimeout: "Verificarea a durat prea mult",
      retry: "Încearcă din nou",
      featuredStory: "Poveste recomandată"
    }),
    cs: Object.freeze({
      documentTitle: "Zadejte kód a sledujte dál | DramaWave",
      brandPill: "Hledání podle kódu",
      eyebrow: "Pokračujte v příběhu",
      titleLead: "Zadejte kód a ",
      titleAccent: "sledujte dál",
      searchAria: "Hledat pomocí krátkého kódu nebo DramaWave Content ID",
      searchLabel: "Krátký kód nebo Content ID",
      exactMatch: "Přesná shoda",
      placeholder: "např. A7K2 nebo Content ID",
      findAria: "Najít příběh",
      helperInitial: "Kódy mají čtyři písmena nebo číslice. Zadejte celý Content ID o délce 10–32 znaků.",
      matchConfirmed: "Shoda potvrzena",
      continueText: "Otevřít odpovídající příběh",
      recentTitle: "Nedávno doporučené",
      recentNote: "Přejeďte, přetáhněte nebo použijte šipky",
      controlsAria: "Ovládání doporučených příběhů",
      previousAria: "Předchozí doporučené příběhy",
      nextAria: "Další doporučené příběhy",
      storiesAria: "Doporučené příběhy",
      footer: "Příběh a cíl se ověří před otevřením DramaWave",
      yesterdayTop: "Nejlepší příběhy včerejška",
      featuredTitle: "Doporučené příběhy",
      featuredTimeout: "Načítání doporučených příběhů trvalo příliš dlouho",
      openStoryAria: "Otevřít {title} v DramaWave",
      coverAlt: "Obálka {title}",
      episodes: "{count} dílů",
      descriptionUnavailable: "Popis příběhu zatím není k dispozici.",
      matchMessage: "Shoda potvrzena. Klepnutím níže pokračujte v DramaWave.",
      inputInvalid: "Zadejte čtyřznakový kód nebo celý Content ID o délce 10–32 znaků.",
      finding: "Hledáme a ověřujeme váš příběh…",
      notFound: "Tento kód nebo Content ID nebyl nalezen. Zkontrolujte ho a zkuste to znovu.",
      tooMany: "Příliš mnoho hledání. Chvíli počkejte a zkuste to znovu.",
      searchTimeout: "Hledání trvalo příliš dlouho. Zkuste to znovu.",
      searchUnavailable: "Hledání je dočasně nedostupné. Zkuste to znovu.",
      helperIdle: "Zadejte čtyřznakový kód nebo celý Content ID.",
      opening: "Otevírá se DramaWave",
      checking: "Ověřování příběhu…",
      storyUnavailable: "Příběh není dostupný",
      storyCheckTimeout: "Ověření trvalo příliš dlouho",
      retry: "Zkuste to znovu",
      featuredStory: "Doporučený příběh"
    }),
    tl: Object.freeze({
      documentTitle: "Ilagay ang code at magpatuloy manood | DramaWave",
      brandPill: "Paghahanap gamit ang code",
      eyebrow: "Ipagpatuloy ang kuwento",
      titleLead: "Ilagay ang code at ",
      titleAccent: "magpatuloy manood",
      searchAria: "Maghanap gamit ang maikling code o DramaWave Content ID",
      searchLabel: "Maikling code o Content ID",
      exactMatch: "Eksaktong tugma",
      placeholder: "hal. A7K2 o Content ID",
      findAria: "Hanapin ang kuwento",
      helperInitial: "Apat na letra o numero ang code. Ilagay ang buong 10–32 character na Content ID.",
      matchConfirmed: "Nakumpirma ang tugma",
      continueText: "Buksan ang katugmang kuwento",
      recentTitle: "Kamakailang tampok",
      recentNote: "Mag-swipe, mag-drag, o gamitin ang mga arrow",
      controlsAria: "Mga kontrol ng tampok na kuwento",
      previousAria: "Mga nakaraang tampok na kuwento",
      nextAria: "Mga susunod na tampok na kuwento",
      storiesAria: "Mga tampok na kuwento",
      footer: "Sinusuri ang kuwento at destinasyon bago buksan ang DramaWave",
      yesterdayTop: "Mga nangungunang kuwento kahapon",
      featuredTitle: "Mga tampok na kuwento",
      featuredTimeout: "Masyadong matagal ang pag-load ng mga tampok na kuwento",
      openStoryAria: "Buksan ang {title} sa DramaWave",
      coverAlt: "Cover ng {title}",
      episodes: "{count} episode",
      descriptionUnavailable: "Wala pang paglalarawan ng kuwento.",
      matchMessage: "Nakumpirma ang tugma. I-tap sa ibaba para magpatuloy sa DramaWave.",
      inputInvalid: "Maglagay ng apat na character na code o buong 10–32 character na Content ID.",
      finding: "Hinahanap at sinusuri ang iyong kuwento…",
      notFound: "Hindi nakita ang code o Content ID na iyon. Suriin at subukang muli.",
      tooMany: "Masyadong maraming paghahanap. Maghintay sandali at subukang muli.",
      searchTimeout: "Masyadong matagal ang paghahanap. Subukang muli.",
      searchUnavailable: "Pansamantalang hindi available ang paghahanap. Subukang muli.",
      helperIdle: "Maglagay ng apat na character na code o buong Content ID.",
      opening: "Binubuksan ang DramaWave",
      checking: "Sinusuri ang kuwento…",
      storyUnavailable: "Hindi available ang kuwento",
      storyCheckTimeout: "Masyadong matagal ang pagsusuri",
      retry: "Subukang muli",
      featuredStory: "Tampok na kuwento"
    }),
    hi: Object.freeze({
      documentTitle: "कोड डालें और देखते रहें | DramaWave",
      brandPill: "कोड से खोजें",
      eyebrow: "कहानी जारी रखें",
      titleLead: "कोड डालें और ",
      titleAccent: "देखते रहें",
      searchAria: "छोटे कोड या DramaWave Content ID से खोजें",
      searchLabel: "छोटा कोड या Content ID",
      exactMatch: "बिल्कुल सही मिलान",
      placeholder: "जैसे A7K2 या Content ID",
      findAria: "कहानी खोजें",
      helperInitial: "कोड में चार अक्षर या अंक होते हैं। Content ID के सभी 10–32 वर्ण डालें।",
      matchConfirmed: "मिलान की पुष्टि हुई",
      continueText: "मिलती हुई कहानी खोलें",
      recentTitle: "हाल में प्रदर्शित",
      recentNote: "स्वाइप करें, खींचें या तीरों का उपयोग करें",
      controlsAria: "फ़ीचर्ड कहानियों के नियंत्रण",
      previousAria: "पिछली फ़ीचर्ड कहानियाँ",
      nextAria: "अगली फ़ीचर्ड कहानियाँ",
      storiesAria: "फ़ीचर्ड कहानियाँ",
      footer: "DramaWave खुलने से पहले कहानी और गंतव्य की पुष्टि की जाती है",
      yesterdayTop: "कल की लोकप्रिय कहानियाँ",
      featuredTitle: "फ़ीचर्ड कहानियाँ",
      featuredTimeout: "फ़ीचर्ड कहानियाँ लोड होने में बहुत समय लगा",
      openStoryAria: "{title} को DramaWave में खोलें",
      coverAlt: "{title} का कवर",
      episodes: "{count} एपिसोड",
      descriptionUnavailable: "कहानी का विवरण अभी उपलब्ध नहीं है।",
      matchMessage: "मिलान की पुष्टि हुई। DramaWave में आगे देखने के लिए नीचे टैप करें।",
      inputInvalid: "चार वर्णों का कोड या 10–32 वर्णों वाला पूरा Content ID डालें।",
      finding: "आपकी कहानी खोजी और जाँची जा रही है…",
      notFound: "वह कोड या Content ID नहीं मिला। जाँचें और फिर कोशिश करें।",
      tooMany: "बहुत ज़्यादा खोजें की गईं। थोड़ा इंतज़ार करके फिर कोशिश करें।",
      searchTimeout: "कहानी खोजने में बहुत समय लगा। फिर कोशिश करें।",
      searchUnavailable: "खोज अभी अस्थायी रूप से उपलब्ध नहीं है। फिर कोशिश करें।",
      helperIdle: "चार वर्णों का कोड या पूरा Content ID डालें।",
      opening: "DramaWave खोला जा रहा है",
      checking: "कहानी जाँची जा रही है…",
      storyUnavailable: "कहानी उपलब्ध नहीं है",
      storyCheckTimeout: "कहानी की जाँच में बहुत समय लगा",
      retry: "फिर कोशिश करें",
      featuredStory: "फ़ीचर्ड कहानी"
    }),
    el: Object.freeze({
      documentTitle: "Εισαγάγετε τον κωδικό και συνεχίστε να βλέπετε | DramaWave",
      brandPill: "Αναζήτηση κωδικού",
      eyebrow: "Συνεχίστε την ιστορία",
      titleLead: "Εισαγάγετε τον κωδικό και ",
      titleAccent: "συνεχίστε να βλέπετε",
      searchAria: "Αναζήτηση με σύντομο κωδικό ή DramaWave Content ID",
      searchLabel: "Σύντομος κωδικός ή Content ID",
      exactMatch: "Ακριβής αντιστοίχιση",
      placeholder: "π.χ. A7K2 ή Content ID",
      findAria: "Εύρεση ιστορίας",
      helperInitial: "Οι κωδικοί έχουν τέσσερα γράμματα ή ψηφία. Εισαγάγετε ολόκληρο το Content ID των 10–32 χαρακτήρων.",
      matchConfirmed: "Η αντιστοίχιση επιβεβαιώθηκε",
      continueText: "Άνοιγμα της αντίστοιχης ιστορίας",
      recentTitle: "Πρόσφατες προτάσεις",
      recentNote: "Σύρετε ή χρησιμοποιήστε τα βέλη",
      controlsAria: "Στοιχεία ελέγχου προτεινόμενων ιστοριών",
      previousAria: "Προηγούμενες προτεινόμενες ιστορίες",
      nextAria: "Επόμενες προτεινόμενες ιστορίες",
      storiesAria: "Προτεινόμενες ιστορίες",
      footer: "Η ιστορία και ο προορισμός επαληθεύονται πριν ανοίξει το DramaWave",
      yesterdayTop: "Οι κορυφαίες ιστορίες χθες",
      featuredTitle: "Προτεινόμενες ιστορίες",
      featuredTimeout: "Η φόρτωση των προτεινόμενων ιστοριών άργησε πολύ",
      openStoryAria: "Άνοιγμα του {title} στο DramaWave",
      coverAlt: "Εξώφυλλο του {title}",
      episodes: "{count} επεισόδια",
      descriptionUnavailable: "Η περιγραφή της ιστορίας δεν είναι ακόμη διαθέσιμη.",
      matchMessage: "Η αντιστοίχιση επιβεβαιώθηκε. Πατήστε παρακάτω για να συνεχίσετε στο DramaWave.",
      inputInvalid: "Εισαγάγετε κωδικό τεσσάρων χαρακτήρων ή ολόκληρο το Content ID των 10–32 χαρακτήρων.",
      finding: "Αναζήτηση και επαλήθευση της ιστορίας σας…",
      notFound: "Δεν βρέθηκε αυτός ο κωδικός ή το Content ID. Ελέγξτε το και δοκιμάστε ξανά.",
      tooMany: "Πάρα πολλές αναζητήσεις. Περιμένετε λίγο και δοκιμάστε ξανά.",
      searchTimeout: "Η αναζήτηση άργησε πολύ. Δοκιμάστε ξανά.",
      searchUnavailable: "Η αναζήτηση δεν είναι προσωρινά διαθέσιμη. Δοκιμάστε ξανά.",
      helperIdle: "Εισαγάγετε κωδικό τεσσάρων χαρακτήρων ή ολόκληρο το Content ID.",
      opening: "Άνοιγμα του DramaWave",
      checking: "Έλεγχος ιστορίας…",
      storyUnavailable: "Η ιστορία δεν είναι διαθέσιμη",
      storyCheckTimeout: "Ο έλεγχος άργησε πολύ",
      retry: "Δοκιμάστε ξανά",
      featuredStory: "Προτεινόμενη ιστορία"
    }),
    ms: Object.freeze({
      documentTitle: "Masukkan kod dan teruskan menonton | DramaWave",
      brandPill: "Carian kod",
      eyebrow: "Teruskan cerita",
      titleLead: "Masukkan kod dan ",
      titleAccent: "teruskan menonton",
      searchAria: "Cari menggunakan kod pendek atau DramaWave Content ID",
      searchLabel: "Kod pendek atau Content ID",
      exactMatch: "Padanan tepat",
      placeholder: "cth. A7K2 atau Content ID",
      findAria: "Cari cerita",
      helperInitial: "Kod mengandungi empat huruf atau nombor. Masukkan nilai penuh Content ID sepanjang 10–32 aksara.",
      matchConfirmed: "Padanan disahkan",
      continueText: "Buka cerita yang sepadan",
      recentTitle: "Pilihan terkini",
      recentNote: "Leret, seret atau gunakan anak panah",
      controlsAria: "Kawalan cerita pilihan",
      previousAria: "Cerita pilihan sebelumnya",
      nextAria: "Cerita pilihan seterusnya",
      storiesAria: "Cerita pilihan",
      footer: "Cerita dan destinasi disahkan sebelum DramaWave dibuka",
      yesterdayTop: "Cerita teratas semalam",
      featuredTitle: "Cerita pilihan",
      featuredTimeout: "Cerita pilihan mengambil masa terlalu lama untuk dimuatkan",
      openStoryAria: "Buka {title} dalam DramaWave",
      coverAlt: "Muka depan {title}",
      episodes: "{count} episod",
      descriptionUnavailable: "Penerangan cerita belum tersedia.",
      matchMessage: "Padanan disahkan. Ketik di bawah untuk meneruskan dalam DramaWave.",
      inputInvalid: "Masukkan kod empat aksara atau Content ID penuh sepanjang 10–32 aksara.",
      finding: "Mencari dan mengesahkan cerita anda…",
      notFound: "Kod atau Content ID itu tidak ditemui. Semak dan cuba lagi.",
      tooMany: "Terlalu banyak carian. Tunggu sebentar dan cuba lagi.",
      searchTimeout: "Carian mengambil masa terlalu lama. Cuba lagi.",
      searchUnavailable: "Carian tidak tersedia buat sementara waktu. Cuba lagi.",
      helperIdle: "Masukkan kod empat aksara atau Content ID penuh.",
      opening: "Membuka DramaWave",
      checking: "Menyemak cerita…",
      storyUnavailable: "Cerita tidak tersedia",
      storyCheckTimeout: "Semakan mengambil masa terlalu lama",
      retry: "Cuba lagi",
      featuredStory: "Cerita pilihan"
    }),
    "zh-hans": Object.freeze({
      documentTitle: "输入代码继续观看 | DramaWave",
      brandPill: "代码搜索",
      eyebrow: "继续故事",
      titleLead: "输入代码，",
      titleAccent: "继续观看",
      searchAria: "使用短代码或 DramaWave Content ID 搜索",
      searchLabel: "短代码或 Content ID",
      exactMatch: "精确匹配",
      placeholder: "例如 A7K2 或 Content ID",
      findAria: "查找短剧",
      helperInitial: "代码由 4 位字母或数字组成。Content ID 需输入完整的 10–32 位值。",
      matchConfirmed: "匹配成功",
      continueText: "打开匹配短剧",
      recentTitle: "推荐短剧",
      recentNote: "滑动、拖动或使用箭头",
      controlsAria: "推荐短剧控制",
      previousAria: "上一组推荐短剧",
      nextAria: "下一组推荐短剧",
      storiesAria: "推荐短剧",
      footer: "打开 DramaWave 前会验证短剧和跳转地址",
      yesterdayTop: "昨日热门短剧",
      featuredTitle: "推荐短剧",
      featuredTimeout: "推荐短剧加载超时",
      openStoryAria: "在 DramaWave 中打开 {title}",
      coverAlt: "{title} 封面",
      episodes: "{count} 集",
      descriptionUnavailable: "剧情简介暂不可用。",
      matchMessage: "匹配成功，点击下方按钮继续在 DramaWave 观看。",
      inputInvalid: "请输入 4 位代码或完整的 10–32 位 Content ID。",
      finding: "正在查找并验证短剧…",
      notFound: "未找到该代码或 Content ID，请检查后重试。",
      tooMany: "搜索次数过多，请稍后重试。",
      searchTimeout: "搜索超时，请重试。",
      searchUnavailable: "暂时无法搜索短剧，请重试。",
      helperIdle: "请输入 4 位代码或完整的 Content ID。",
      opening: "正在打开 DramaWave",
      checking: "正在验证短剧…",
      storyUnavailable: "短剧不可用",
      storyCheckTimeout: "短剧验证超时",
      retry: "请重试",
      featuredStory: "推荐短剧"
    }),
    "zh-tw": Object.freeze({
      documentTitle: "輸入代碼繼續觀看 | DramaWave",
      brandPill: "代碼搜尋",
      eyebrow: "繼續故事",
      titleLead: "輸入代碼，",
      titleAccent: "繼續觀看",
      searchAria: "使用短代碼或 DramaWave Content ID 搜尋",
      searchLabel: "短代碼或 Content ID",
      exactMatch: "精確匹配",
      placeholder: "例如 A7K2 或 Content ID",
      findAria: "尋找短劇",
      helperInitial: "代碼由 4 位字母或數字組成。Content ID 需輸入完整的 10–32 位值。",
      matchConfirmed: "匹配成功",
      continueText: "開啟匹配短劇",
      recentTitle: "推薦短劇",
      recentNote: "滑動、拖曳或使用箭頭",
      controlsAria: "推薦短劇控制",
      previousAria: "上一組推薦短劇",
      nextAria: "下一組推薦短劇",
      storiesAria: "推薦短劇",
      footer: "開啟 DramaWave 前會驗證短劇和跳轉網址",
      yesterdayTop: "昨日熱門短劇",
      featuredTitle: "推薦短劇",
      featuredTimeout: "推薦短劇載入逾時",
      openStoryAria: "在 DramaWave 中開啟 {title}",
      coverAlt: "{title} 封面",
      episodes: "{count} 集",
      descriptionUnavailable: "劇情簡介暫不可用。",
      matchMessage: "匹配成功，點擊下方按鈕繼續在 DramaWave 觀看。",
      inputInvalid: "請輸入 4 位代碼或完整的 10–32 位 Content ID。",
      finding: "正在尋找並驗證短劇…",
      notFound: "找不到該代碼或 Content ID，請檢查後重試。",
      tooMany: "搜尋次數過多，請稍後重試。",
      searchTimeout: "搜尋逾時，請重試。",
      searchUnavailable: "暫時無法搜尋短劇，請重試。",
      helperIdle: "請輸入 4 位代碼或完整的 Content ID。",
      opening: "正在開啟 DramaWave",
      checking: "正在驗證短劇…",
      storyUnavailable: "短劇不可用",
      storyCheckTimeout: "短劇驗證逾時",
      retry: "請重試",
      featuredStory: "推薦短劇"
    })
  });

  function normalizeLanguageTag(value) {
    const normalized = String(value == null ? "" : value)
      .trim()
      .toLowerCase()
      .replace(/_/g, "-");
    if (!normalized || normalized.length > 32 ||
        !LANGUAGE_TAG_PATTERN.test(normalized)) {
      return "";
    }
    return normalized;
  }

  function localeCandidates(value) {
    const language = normalizeLanguageTag(value);
    if (!language) {
      return Object.freeze([]);
    }
    if (language === "zh" || language.startsWith("zh-")) {
      const traditional = /(?:^|-)(?:tw|hk|mo|hant)(?:-|$)/.test(language);
      return Object.freeze([traditional ? "zh-tw" : "zh-hans"]);
    }
    const exact = language === "fil" ? "tl" : language;
    const rawBase = language.split("-")[0];
    const base = rawBase === "fil" ? "tl" : rawBase;
    return Object.freeze(exact === base ? [exact] : [exact, base]);
  }

  function getBrowserLanguages(navigatorValue) {
    const source = navigatorValue || {};
    const values = [];
    if (Array.isArray(source.languages)) {
      values.push(...source.languages);
    }
    if (source.language) {
      values.push(source.language);
    }
    const result = [];
    for (const value of values) {
      const language = normalizeLanguageTag(value);
      if (language && !result.includes(language)) {
        result.push(language);
      }
    }
    return Object.freeze(result);
  }

  function resolveUiLocale(browserLanguages) {
    const values = Array.isArray(browserLanguages) ? browserLanguages : [];
    for (const value of values) {
      for (const candidate of localeCandidates(value)) {
        if (Object.prototype.hasOwnProperty.call(COPY, candidate)) {
          return candidate;
        }
      }
    }
    return "en";
  }

  function rankingLanguageCandidates(browserLanguages) {
    const values = Array.isArray(browserLanguages) ? browserLanguages : [];
    const result = [];
    for (const value of values) {
      const language = normalizeLanguageTag(value);
      if (!language) {
        continue;
      }
      const uiSupported = localeCandidates(language).some(function (candidate) {
        return Object.prototype.hasOwnProperty.call(COPY, candidate);
      });
      if (!uiSupported) {
        continue;
      }
      let candidates;
      if (language === "zh" || language.startsWith("zh-")) {
        candidates = ["zh-tw"];
      } else {
        const exact = language === "fil" ? "tl" : language;
        const rawBase = language.split("-")[0];
        const base = rawBase === "fil" ? "tl" : rawBase;
        candidates = exact === base ? [exact] : [exact, base];
      }
      for (const candidate of candidates) {
        if (!result.includes(candidate)) {
          result.push(candidate);
        }
      }
    }
    if (!result.includes("en")) {
      result.push("en");
    }
    return Object.freeze(result);
  }

  function copyText(locale, key, values) {
    const selected = COPY[locale] || EN_COPY;
    let text = String(
      Object.prototype.hasOwnProperty.call(selected, key)
        ? selected[key]
        : EN_COPY[key] || ""
    );
    for (const [name, value] of Object.entries(values || {})) {
      text = text.split("{" + name + "}").join(String(value));
    }
    return text;
  }

  function buildFallbackFeatured(locale) {
    const language = normalizeLanguageTag(locale) || "en";
    return Object.freeze(Array.from({ length: 5 }, function (_value, index) {
      return Object.freeze({
        title: copyText(locale, "featuredStory") + " " + (index + 1),
        language: language.toUpperCase(),
        cover_url: ""
      });
    }));
  }

  function normalizeQuery(value) {
    const query = String(value == null ? "" : value).trim();
    const upper = query.toUpperCase();
    if (CODE_PATTERN.test(upper)) {
      return Object.freeze({
        query: upper,
        queryType: "code"
      });
    }
    if (CONTENT_ID_PATTERN.test(query)) {
      return Object.freeze({
        query,
        queryType: "content_id"
      });
    }
    throw new TypeError("Enter a four-character code or a complete Content ID");
  }

  function requireContentId(value) {
    const contentId = String(value == null ? "" : value);
    if (!CONTENT_ID_PATTERN.test(contentId)) {
      throw new TypeError("Invalid DramaWave content_id");
    }
    return contentId;
  }

  function normalizeSource(value) {
    const source = String(value || "");
    if (source !== SEARCH_SOURCE && source !== FEATURED_SOURCE) {
      throw new TypeError("Invalid TT code resolver source");
    }
    return source;
  }

  function normalizeOrigin(value) {
    const parsed = new URL(String(value || ""));
    if (
      (parsed.protocol !== "https:" && parsed.protocol !== "http:") ||
      parsed.username ||
      parsed.password ||
      parsed.hash
    ) {
      throw new TypeError("Invalid resolver origin");
    }
    return parsed.origin;
  }

  function buildCodeResolverUrl(query, source, origin) {
    const normalized = normalizeQuery(query);
    const url = new URL(CODE_RESOLVER_PATH, normalizeOrigin(origin));
    url.searchParams.set("query", normalized.query);
    url.searchParams.set("source", normalizeSource(source));
    return url.href;
  }

  function buildFeaturedUrl(origin) {
    return new URL(FEATURED_PATH, normalizeOrigin(origin)).href;
  }

  function normalizeSafeToken(value, label) {
    const token = String(value || "");
    if (!SAFE_TOKEN_PATTERN.test(token)) {
      throw new TypeError("Invalid " + label);
    }
    return token;
  }

  function validateTargetUrl(value, contentId) {
    const expectedContentId = requireContentId(contentId);
    const raw = String(value || "");
    if (!raw || raw.length > 8192 || raw.trim() !== raw) {
      throw new TypeError("Invalid TT target URL");
    }

    let target;
    try {
      target = new URL(raw);
    } catch (_error) {
      throw new TypeError("Invalid TT target URL");
    }

    if (
      target.protocol !== "https:" ||
      target.origin !== TARGET_ORIGIN ||
      target.pathname !== TARGET_PATH ||
      target.username ||
      target.password ||
      target.port ||
      target.hash
    ) {
      throw new TypeError("Untrusted TT target URL");
    }

    const seen = new Set();
    for (const [key] of target.searchParams.entries()) {
      if (!TARGET_PARAM_KEY_SET.has(key) || seen.has(key)) {
        throw new TypeError("Untrusted TT target parameters");
      }
      seen.add(key);
    }

    for (const requiredKey of ["af_dp", "c", "af_c_id"]) {
      const values = target.searchParams.getAll(requiredKey);
      if (values.length !== 1 || !values[0]) {
        throw new TypeError("Incomplete TT target parameters");
      }
    }
    if (target.searchParams.get("af_dp") !== expectedContentId) {
      throw new TypeError("TT target content_id mismatch");
    }
    const channel = target.searchParams.get("af_channel");
    if (
      channel &&
      channel !== "TT" &&
      channel !== "IG" &&
      channel !== SEARCH_SOURCE &&
      channel !== FEATURED_SOURCE
    ) {
      throw new TypeError("Invalid TT target channel");
    }
    return target.href;
  }

  function normalizeCodeResolvePayload(payload, expectedQuery, expectedSource) {
    if (!payload || payload.found !== true || !payload.item ||
        typeof payload.item !== "object" || Array.isArray(payload.item)) {
      throw new TypeError("Invalid TT code resolver payload");
    }
    const normalizedQuery = normalizeQuery(expectedQuery);
    const contentId = requireContentId(payload.item.content_id);
    const queryType = normalizeSafeToken(payload.item.query_type, "query_type");
    const routeMode = normalizeSafeToken(payload.item.route_mode, "route_mode");
    const source = normalizeSource(expectedSource);
    if (queryType !== normalizedQuery.queryType) {
      throw new TypeError("TT code resolver query_type mismatch");
    }
    if (queryType === "code" && routeMode !== "code_exact") {
      throw new TypeError("Code result must use the frozen TT target");
    }
    if (
      queryType === "content_id" &&
      routeMode !== "published_clone" &&
      routeMode !== "generic_fallback"
    ) {
      throw new TypeError("Content ID result has an invalid route_mode");
    }
    const targetUrl = validateTargetUrl(payload.item.target_url, contentId);
    const channel = new URL(targetUrl).searchParams.get("af_channel");
    if (queryType === "code" && channel !== "TT") {
      throw new TypeError("Code result must preserve the TT channel");
    }
    if (queryType === "content_id" && channel !== source) {
      throw new TypeError("Content ID result source mismatch");
    }
    const drama = normalizeDramaPayload({
      found: true,
      data: payload.item
    }, contentId);
    return Object.freeze({
      content_id: contentId,
      target_url: targetUrl,
      query_type: queryType,
      route_mode: routeMode,
      title: drama.title,
      description: drama.description,
      cover_url: drama.cover_url,
      language: drama.language,
      episode_count: drama.episode_count
    });
  }

  function normalizeDramaPayload(payload, expectedContentId) {
    const contentId = requireContentId(expectedContentId);
    if (!payload || payload.found !== true || !payload.data ||
        typeof payload.data !== "object" || Array.isArray(payload.data) ||
        payload.data.content_id !== contentId) {
      throw new TypeError("Invalid DramaWave resolver payload");
    }
    return Object.freeze({
      content_id: contentId,
      title: String(payload.data.title || contentId).trim().slice(0, 240),
      description: String(payload.data.description || "").trim().slice(0, 1600),
      cover_url: String(payload.data.cover_url || "").trim(),
      language: String(payload.data.language || "").trim().slice(0, 32),
      episode_count: Math.max(0, Number(payload.data.episode_count) || 0)
    });
  }

  function isSafeFeaturedCover(value) {
    try {
      const url = new URL(String(value || ""));
      return (
        url.protocol === "https:" &&
        !url.username &&
        !url.password &&
        !url.port &&
        !url.hash &&
        FEATURED_COVER_HOSTS.has(url.hostname)
      );
    } catch (_error) {
      return false;
    }
  }

  function shanghaiYesterday(nowMs) {
    const currentMs = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
    const shifted = new Date(currentMs + (8 * 60 * 60 * 1000));
    const midnight = Date.UTC(
      shifted.getUTCFullYear(),
      shifted.getUTCMonth(),
      shifted.getUTCDate()
    );
    const previous = new Date(midnight - (24 * 60 * 60 * 1000));
    const year = previous.getUTCFullYear();
    const month = String(previous.getUTCMonth() + 1).padStart(2, "0");
    const day = String(previous.getUTCDate()).padStart(2, "0");
    return year + "-" + month + "-" + day;
  }

  function normalizeFeaturedPayload(payload, nowMs) {
    if (
      !payload ||
      Number(payload.schema_version) !== 1 ||
      !/^\d{4}-\d{2}-\d{2}$/.test(String(payload.source_date || "")) ||
      !Array.isArray(payload.items) ||
      payload.items.length !== 5
    ) {
      throw new TypeError("Invalid featured stories payload");
    }

    const generatedAtMs = Date.parse(String(payload.generated_at || ""));
    const currentMs = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
    const sourceDateMs = Date.parse(String(payload.source_date) + "T00:00:00Z");
    const yesterdayMs = Date.parse(shanghaiYesterday(currentMs) + "T00:00:00Z");
    if (
      !Number.isFinite(generatedAtMs) ||
      generatedAtMs - currentMs > FEATURED_MAX_FUTURE_SKEW_MS ||
      currentMs - generatedAtMs > FEATURED_MAX_STALE_MS ||
      !Number.isFinite(sourceDateMs) ||
      sourceDateMs > yesterdayMs ||
      yesterdayMs - sourceDateMs > FEATURED_MAX_STALE_MS
    ) {
      throw new TypeError("Featured stories payload is stale");
    }

    const items = [];
    const seen = new Set();
    for (const source of payload.items) {
      const contentId = String(source && source.content_id || "");
      const title = String(source && source.title || "").trim().slice(0, 240);
      const coverUrl = String(source && source.cover_url || "").trim();
      if (
        !source ||
        Object.prototype.hasOwnProperty.call(source, "spend") ||
        Object.prototype.hasOwnProperty.call(source, "spend_n") ||
        !CONTENT_ID_PATTERN.test(contentId) ||
        seen.has(contentId) ||
        !title ||
        !isSafeFeaturedCover(coverUrl)
      ) {
        throw new TypeError("Featured stories payload is incomplete");
      }
      seen.add(contentId);
      items.push(Object.freeze({
        content_id: contentId,
        title,
        cover_url: coverUrl,
        language: String(source.language || "").trim().slice(0, 32),
        episode_count: Math.max(0, Number(source.episode_count) || 0)
      }));
    }

    return Object.freeze({
      source_date: String(payload.source_date),
      generated_at: String(payload.generated_at),
      items: Object.freeze(items)
    });
  }

  function hasExactKeys(value, expected) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return false;
    }
    const actual = Object.keys(value).sort();
    const wanted = Array.from(expected).sort();
    return actual.length === wanted.length &&
      actual.every(function (key, index) { return key === wanted[index]; });
  }

  function containsPrivateSpendKey(value) {
    if (Array.isArray(value)) {
      return value.some(containsPrivateSpendKey);
    }
    if (!value || typeof value !== "object") {
      return false;
    }
    return Object.entries(value).some(function ([key, item]) {
      return ["spend", "spend_n"].includes(String(key).trim().toLowerCase()) ||
        containsPrivateSpendKey(item);
    });
  }

  function validateFeaturedFreshness(payload, nowMs) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(payload.source_date || ""))) {
      throw new TypeError("Invalid featured stories source date");
    }
    const generatedAtMs = Date.parse(String(payload.generated_at || ""));
    const currentMs = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
    const sourceDateMs = Date.parse(String(payload.source_date) + "T00:00:00Z");
    const yesterdayMs = Date.parse(shanghaiYesterday(currentMs) + "T00:00:00Z");
    if (
      !Number.isFinite(generatedAtMs) ||
      generatedAtMs - currentMs > FEATURED_MAX_FUTURE_SKEW_MS ||
      currentMs - generatedAtMs > FEATURED_MAX_STALE_MS ||
      !Number.isFinite(sourceDateMs) ||
      sourceDateMs > yesterdayMs ||
      yesterdayMs - sourceDateMs > FEATURED_MAX_STALE_MS
    ) {
      throw new TypeError("Featured stories payload is stale");
    }
  }

  function normalizeFeaturedBundle(payload, browserLanguages, nowMs) {
    const topLevelKeys = [
      "schema_version",
      "source_date",
      "generated_at",
      "default_language",
      "rankings"
    ];
    if (
      !hasExactKeys(payload, topLevelKeys) ||
      payload.schema_version !== 2 ||
      payload.default_language !== "en" ||
      !payload.rankings ||
      typeof payload.rankings !== "object" ||
      Array.isArray(payload.rankings) ||
      containsPrivateSpendKey(payload)
    ) {
      throw new TypeError("Invalid featured language bundle");
    }
    validateFeaturedFreshness(payload, nowMs);

    const rankingEntries = Object.entries(payload.rankings);
    if (
      rankingEntries.length < 1 ||
      rankingEntries.length > MAX_FEATURED_LANGUAGE_BUCKETS
    ) {
      throw new TypeError("Invalid featured language bucket count");
    }
    const rankings = Object.create(null);
    const seenContentIds = new Set();
    const itemKeys = [
      "content_id",
      "title",
      "cover_url",
      "language",
      "episode_count"
    ];
    for (const [rawLanguage, rawItems] of rankingEntries) {
      const language = normalizeLanguageTag(rawLanguage);
      if (!language || !FEATURED_BUCKET_LANGUAGE_PATTERN.test(language) ||
          language !== rawLanguage ||
          !Array.isArray(rawItems) || rawItems.length !== 5) {
        throw new TypeError("Invalid featured language bucket");
      }
      const items = [];
      for (const source of rawItems) {
        if (!hasExactKeys(source, itemKeys)) {
          throw new TypeError("Featured story fields are incomplete");
        }
        const contentId = String(source.content_id || "");
        const title = String(source.title || "").trim().slice(0, 240);
        const coverUrl = String(source.cover_url || "").trim();
        const itemLanguage = normalizeLanguageTag(source.language);
        const episodeCount = source.episode_count;
        if (
          !CONTENT_ID_PATTERN.test(contentId) ||
          seenContentIds.has(contentId) ||
          !title ||
          !isSafeFeaturedCover(coverUrl) ||
          itemLanguage !== language || source.language !== language ||
          typeof episodeCount !== "number" ||
          !Number.isInteger(episodeCount) ||
          episodeCount < 0
        ) {
          throw new TypeError("Featured story is invalid");
        }
        seenContentIds.add(contentId);
        items.push(Object.freeze({
          content_id: contentId,
          title,
          cover_url: coverUrl,
          language,
          episode_count: episodeCount
        }));
      }
      rankings[language] = Object.freeze(items);
    }
    if (!Object.prototype.hasOwnProperty.call(rankings, "en")) {
      throw new TypeError("English featured stories are required");
    }

    const candidates = rankingLanguageCandidates(browserLanguages);
    const requestedLanguage = candidates[0] || "en";
    const selectedLanguage = candidates.find(function (language) {
      return Object.prototype.hasOwnProperty.call(rankings, language);
    }) || "en";
    const firstBrowserLanguage = Array.isArray(browserLanguages)
      ? normalizeLanguageTag(browserLanguages[0])
      : "";
    const requestedBaseLanguage = !firstBrowserLanguage
      ? "en"
      : firstBrowserLanguage === "zh" || firstBrowserLanguage.startsWith("zh-")
        ? "zh-tw"
        : firstBrowserLanguage.split("-")[0] === "fil"
          ? "tl"
          : firstBrowserLanguage.split("-")[0];
    return Object.freeze({
      source_date: String(payload.source_date),
      generated_at: String(payload.generated_at),
      requested_language: requestedLanguage,
      language: selectedLanguage,
      fallback: selectedLanguage === "en" && requestedBaseLanguage !== "en",
      items: rankings[selectedLanguage]
    });
  }

  function createDragTracker(thresholdPx) {
    const threshold = Math.max(1, Number(thresholdPx) || DRAG_THRESHOLD_PX);
    let active = false;
    let startX = 0;
    let startScrollLeft = 0;
    let dragged = false;
    let suppressClick = false;

    return Object.freeze({
      begin(clientX, scrollLeft) {
        active = true;
        startX = Number(clientX) || 0;
        startScrollLeft = Math.max(0, Number(scrollLeft) || 0);
        dragged = false;
        suppressClick = false;
      },
      move(clientX) {
        if (!active) {
          return null;
        }
        const delta = (Number(clientX) || 0) - startX;
        if (Math.abs(delta) >= threshold) {
          dragged = true;
        }
        return Object.freeze({
          dragged,
          scrollLeft: dragged
            ? Math.max(0, startScrollLeft - delta)
            : startScrollLeft
        });
      },
      end() {
        const wasDragged = active && dragged;
        active = false;
        dragged = false;
        suppressClick = wasDragged;
        return wasDragged;
      },
      cancel() {
        active = false;
        dragged = false;
        suppressClick = false;
      },
      consumeSuppressedClick() {
        if (!suppressClick) {
          return false;
        }
        suppressClick = false;
        return true;
      },
      isActive() {
        return active;
      }
    });
  }

  function getCarouselStep(clientWidth) {
    return Math.max(129, Math.round(Math.max(1, Number(clientWidth) || 1) * 0.78));
  }

  function prefersReducedMotion() {
    return Boolean(
      root.matchMedia &&
      root.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function nextFrame(callback) {
    if (typeof root.requestAnimationFrame === "function") {
      root.requestAnimationFrame(callback);
    } else {
      root.setTimeout(callback, 0);
    }
  }

  function attachCarousel(container, previousButton, nextButton) {
    const tracker = createDragTracker(DRAG_THRESHOLD_PX);
    let activePointerId = null;
    let updatePending = false;

    function updateButtons() {
      updatePending = false;
      const maxScroll = Math.max(0, container.scrollWidth - container.clientWidth);
      previousButton.disabled = container.scrollLeft <= 2;
      nextButton.disabled = container.scrollLeft >= maxScroll - 2;
    }

    function queueButtonUpdate() {
      if (updatePending) {
        return;
      }
      updatePending = true;
      nextFrame(updateButtons);
    }

    function scrollByDirection(direction) {
      container.scrollBy({
        left: direction * getCarouselStep(container.clientWidth),
        behavior: prefersReducedMotion() ? "auto" : "smooth"
      });
    }

    function snapToNearestCard() {
      const cards = Array.from(container.querySelectorAll(".story"));
      if (!cards.length) {
        return;
      }
      const firstOffset = cards[0].offsetLeft;
      let closest = 0;
      let distance = Number.POSITIVE_INFINITY;
      for (const card of cards) {
        const candidate = Math.max(0, card.offsetLeft - firstOffset);
        const candidateDistance = Math.abs(candidate - container.scrollLeft);
        if (candidateDistance < distance) {
          distance = candidateDistance;
          closest = candidate;
        }
      }
      container.scrollTo({
        left: closest,
        behavior: prefersReducedMotion() ? "auto" : "smooth"
      });
    }

    function finishPointer(event, cancelled) {
      if (activePointerId === null || event.pointerId !== activePointerId) {
        return;
      }
      const wasDragged = cancelled ? false : tracker.end();
      if (cancelled) {
        tracker.cancel();
      }
      activePointerId = null;
      try {
        if (container.hasPointerCapture(event.pointerId)) {
          container.releasePointerCapture(event.pointerId);
        }
      } catch (_error) {
        // Pointer capture may already have been released by the browser.
      }
      container.classList.remove("is-dragging");
      if (wasDragged) {
        snapToNearestCard();
      }
      queueButtonUpdate();
    }

    previousButton.addEventListener("click", function () {
      scrollByDirection(-1);
    });
    nextButton.addEventListener("click", function () {
      scrollByDirection(1);
    });
    container.addEventListener("scroll", queueButtonUpdate, { passive: true });
    container.addEventListener("dragstart", function (event) {
      event.preventDefault();
    });
    container.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
        return;
      }
      event.preventDefault();
      scrollByDirection(event.key === "ArrowLeft" ? -1 : 1);
    });
    container.addEventListener("pointerdown", function (event) {
      if (
        event.isPrimary === false ||
        (event.pointerType !== "mouse" && event.pointerType !== "pen") ||
        (event.pointerType === "mouse" && event.button !== 0)
      ) {
        return;
      }
      activePointerId = event.pointerId;
      tracker.begin(event.clientX, container.scrollLeft);
    });
    root.addEventListener("pointermove", function (event) {
      if (activePointerId === null || event.pointerId !== activePointerId) {
        return;
      }
      if (event.buttons === 0) {
        finishPointer(event, true);
        return;
      }
      const movement = tracker.move(event.clientX);
      if (!movement) {
        return;
      }
      if (movement.dragged) {
        try {
          if (!container.hasPointerCapture(event.pointerId)) {
            container.setPointerCapture(event.pointerId);
          }
        } catch (_error) {
          // Window-level pointer events still keep the drag state bounded.
        }
        container.classList.add("is-dragging");
        event.preventDefault();
      }
      container.scrollLeft = movement.scrollLeft;
      queueButtonUpdate();
    }, { passive: false });
    root.addEventListener("pointerup", function (event) {
      finishPointer(event, false);
    });
    root.addEventListener("pointercancel", function (event) {
      finishPointer(event, true);
    });
    container.addEventListener("lostpointercapture", function (event) {
      finishPointer(event, false);
    });
    container.addEventListener("click", function (event) {
      if (!tracker.consumeSuppressedClick()) {
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
    }, true);

    if (typeof root.ResizeObserver === "function") {
      const resizeObserver = new root.ResizeObserver(queueButtonUpdate);
      resizeObserver.observe(container);
    } else {
      root.addEventListener("resize", queueButtonUpdate, { passive: true });
    }

    updateButtons();
    return Object.freeze({
      refresh: queueButtonUpdate,
      consumeSuppressedClick: tracker.consumeSuppressedClick
    });
  }

  function responseError(response, payload, fallback) {
    const error = new Error(
      String(payload && payload.message || fallback || "Request failed")
    );
    error.status = response.status;
    error.code = String(
      payload && (payload.code || payload.error) || "resolver_unavailable"
    );
    return error;
  }

  async function fetchPayload(url, signal, cacheMode) {
    const response = await root.fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "omit",
      cache: cacheMode || "no-store",
      signal
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch (_error) {
      payload = {};
    }
    return { response, payload };
  }

  async function resolveCodeQuery(query, source, signal) {
    const normalized = normalizeQuery(query);
    const result = await fetchPayload(
      buildCodeResolverUrl(normalized.query, source, root.location.origin),
      signal,
      "no-store"
    );
    if (!result.response.ok || result.payload.found !== true) {
      throw responseError(
        result.response,
        result.payload,
        "Code or Content ID was not found"
      );
    }
    return normalizeCodeResolvePayload(
      result.payload,
      normalized.query,
      source
    );
  }

  async function resolveAndVerify(query, source, signal) {
    const resolved = await resolveCodeQuery(query, source, signal);
    const route = Object.freeze({
      content_id: resolved.content_id,
      target_url: resolved.target_url,
      query_type: resolved.query_type,
      route_mode: resolved.route_mode
    });
    const drama = Object.freeze({
      content_id: resolved.content_id,
      title: resolved.title,
      description: resolved.description,
      cover_url: resolved.cover_url,
      language: resolved.language,
      episode_count: resolved.episode_count
    });
    return Object.freeze({ route, drama });
  }

  function renderFeaturedStories(container, dramas, locale) {
    container.replaceChildren();
    for (const drama of dramas) {
      const isLinked = Boolean(drama.content_id);
      const card = document.createElement(isLinked ? "a" : "article");
      card.className = isLinked ? "story story-link" : "story";
      card.setAttribute("role", "listitem");
      card.setAttribute("dir", "auto");
      if (isLinked) {
        card.href = "#story-" + drama.content_id;
        card.rel = "noreferrer";
        card.dataset.contentId = drama.content_id;
        card.setAttribute(
          "aria-label",
          copyText(locale, "openStoryAria", { title: drama.title })
        );
      }

      const placeholder = document.createElement("div");
      placeholder.className = "story-cover-placeholder";
      placeholder.textContent =
        String(drama.title || "D").trim().slice(0, 1) || "D";
      placeholder.setAttribute("aria-hidden", "true");

      const image = document.createElement("img");
      image.loading = "lazy";
      image.decoding = "async";
      image.draggable = false;
      image.alt = copyText(locale, "coverAlt", { title: drama.title });
      if (isSafeFeaturedCover(drama.cover_url)) {
        image.src = drama.cover_url;
        image.addEventListener("error", function () {
          image.hidden = true;
        }, { once: true });
      } else {
        image.hidden = true;
      }

      const info = document.createElement("div");
      info.className = "story-info";
      const title = document.createElement("div");
      title.className = "story-title";
      title.textContent = drama.title;
      const tag = document.createElement("div");
      tag.className = "story-tag";
      tag.textContent = String(drama.language || "").toUpperCase();
      info.append(title, tag);
      card.append(placeholder, image, info);
      container.appendChild(card);
    }
  }

  async function loadFeaturedStories(
    container,
    title,
    note,
    carousel,
    locale,
    browserLanguages
  ) {
    const controller = typeof root.AbortController === "function"
      ? new root.AbortController()
      : { signal: undefined, abort() {} };
    let timeoutId = null;
    let timedOut = false;
    timeoutId = root.setTimeout(function () {
      timedOut = true;
      controller.abort();
    }, FEATURED_TIMEOUT_MS);
    try {
      const result = await fetchPayload(
        buildFeaturedUrl(root.location.origin),
        controller.signal,
        "default"
      );
      if (!result.response.ok) {
        throw responseError(
          result.response,
          result.payload,
          copyText(locale, "searchUnavailable")
        );
      }
      const featured = normalizeFeaturedBundle(
        result.payload,
        browserLanguages
      );
      renderFeaturedStories(container, featured.items, locale);
      title.textContent = featured.source_date === shanghaiYesterday()
        ? copyText(locale, "yesterdayTop")
        : copyText(locale, "featuredTitle");
      note.textContent = copyText(locale, "recentNote");
      container.dataset.sourceDate = featured.source_date;
      container.dataset.requestedLanguage = featured.requested_language;
      container.dataset.language = featured.language;
      container.dataset.languageFallback = featured.fallback ? "true" : "false";
      container.dataset.cacheState = "dynamic";
      carousel.refresh();
      return true;
    } catch (error) {
      container.dataset.cacheState = "fallback";
      note.textContent = timedOut
        ? copyText(locale, "featuredTimeout")
        : copyText(locale, "recentNote");
      carousel.refresh();
      return false;
    } finally {
      root.clearTimeout(timeoutId);
    }
  }

  function applyPageCopy(locale) {
    const html = document.documentElement;
    html.lang = locale === "zh-hans"
      ? "zh-Hans"
      : locale === "zh-tw" ? "zh-Hant" : locale;
    html.dir = locale === "ar" ? "rtl" : "ltr";
    document.title = copyText(locale, "documentTitle");

    const setText = function (selector, key) {
      const element = document.querySelector(selector);
      if (element) {
        element.textContent = copyText(locale, key);
      }
    };
    const setAttribute = function (selector, name, key) {
      const element = document.querySelector(selector);
      if (element) {
        element.setAttribute(name, copyText(locale, key));
      }
    };

    setText("#brand-pill", "brandPill");
    setText("#page-eyebrow", "eyebrow");
    const pageTitle = document.querySelector("#page-title");
    const pageTitleAccent = document.querySelector("#page-title-accent");
    if (pageTitle && pageTitleAccent) {
      let lead = Array.from(pageTitle.childNodes).find(function (node) {
        return node.nodeType === 3;
      });
      if (!lead) {
        lead = document.createTextNode("");
        pageTitle.insertBefore(lead, pageTitleAccent);
      }
      lead.nodeValue = copyText(locale, "titleLead");
      pageTitleAccent.textContent = copyText(locale, "titleAccent");
    }
    setAttribute("#search-card", "aria-label", "searchAria");
    setText("#search-label-primary", "searchLabel");
    setText("#search-label-exact", "exactMatch");
    setAttribute("#drama-query", "placeholder", "placeholder");
    setAttribute("#search-button", "aria-label", "findAria");
    setText("#search-helper", "helperInitial");
    setText("#match-badge", "matchConfirmed");
    setText("#continue-text", "continueText");
    setText("#recent-title", "recentTitle");
    setText("#recent-note", "recentNote");
    setAttribute("#carousel-controls", "aria-label", "controlsAria");
    setAttribute("#stories-previous", "aria-label", "previousAria");
    setAttribute("#stories-next", "aria-label", "nextAria");
    setAttribute("#stories", "aria-label", "storiesAria");
    setText("#footer-copy", "footer");
  }

  function initPage() {
    const browserLanguages = getBrowserLanguages(root.navigator);
    const locale = resolveUiLocale(browserLanguages);
    applyPageCopy(locale);
    const searchForm = document.querySelector("#search-form");
    const queryInput = document.querySelector("#drama-query");
    const searchButton = document.querySelector("#search-button");
    const helper = document.querySelector("#search-helper");
    const result = document.querySelector("#result");
    const resultTitle = document.querySelector("#result-title");
    const resultMeta = document.querySelector("#result-meta");
    const resultDescription = document.querySelector("#result-description");
    const resultCover = document.querySelector("#result-cover");
    const resultCoverPlaceholder =
      document.querySelector("#result-cover-placeholder");
    const continueLink = document.querySelector("#continue-link");
    const continueText = document.querySelector("#continue-text");
    const stories = document.querySelector("#stories");
    const featuredTitle = document.querySelector("#recent-title");
    const featuredNote = document.querySelector("#recent-note");
    const previousButton = document.querySelector("#stories-previous");
    const nextButton = document.querySelector("#stories-next");

    if (
      !searchForm ||
      !queryInput ||
      !searchButton ||
      !helper ||
      !result ||
      !resultTitle ||
      !resultMeta ||
      !resultDescription ||
      !resultCover ||
      !resultCoverPlaceholder ||
      !continueLink ||
      !continueText ||
      !stories ||
      !featuredTitle ||
      !featuredNote ||
      !previousButton ||
      !nextButton
    ) {
      return;
    }

    const carousel = attachCarousel(stories, previousButton, nextButton);
    let activeController = null;
    let activeRequest = 0;
    let featuredController = null;

    function setLoading(loading) {
      searchButton.disabled = loading;
      queryInput.setAttribute("aria-busy", loading ? "true" : "false");
    }

    function hideResult() {
      result.classList.remove("visible");
      result.removeAttribute("aria-busy");
      continueLink.removeAttribute("href");
      continueLink.removeAttribute("data-content-id");
      continueLink.removeAttribute("data-query-type");
      resultCover.hidden = true;
      resultCover.removeAttribute("src");
      resultCover.alt = "";
    }

    function showCover(coverUrl, title) {
      resultCoverPlaceholder.textContent =
        String(title || "D").trim().slice(0, 1) || "D";
      resultCover.hidden = true;
      resultCover.removeAttribute("src");
      resultCover.alt = "";
      if (!isSafeFeaturedCover(coverUrl)) {
        return;
      }
      resultCover.onload = function () {
        resultCover.hidden = false;
      };
      resultCover.onerror = function () {
        resultCover.hidden = true;
        resultCover.removeAttribute("src");
      };
      resultCover.alt = copyText(locale, "coverAlt", { title });
      resultCover.src = coverUrl;
    }

    function showDrama(resolved, normalizedQuery) {
      const route = resolved.route;
      const drama = resolved.drama;
      const facts = [];
      if (drama.language) {
        facts.push(drama.language.toUpperCase());
      }
      if (drama.episode_count > 0) {
        facts.push(copyText(locale, "episodes", {
          count: drama.episode_count
        }));
      }
      facts.push("ID " + route.content_id);
      if (route.query_type === "code") {
        facts.push("CODE " + normalizedQuery.query);
      }

      resultTitle.textContent = drama.title || route.content_id;
      resultMeta.textContent = facts.join(" · ");
      resultDescription.textContent =
        drama.description || copyText(locale, "descriptionUnavailable");
      continueLink.href = route.target_url;
      continueLink.dataset.contentId = route.content_id;
      continueLink.dataset.queryType = route.query_type;
      continueText.textContent = copyText(locale, "continueText");
      result.dataset.routeMode = route.route_mode;
      result.dataset.queryType = route.query_type;
      result.classList.add("visible");
      helper.classList.remove("error");
      helper.textContent = copyText(locale, "matchMessage");
      showCover(drama.cover_url, drama.title || route.content_id);
    }

    async function prepareDrama() {
      let normalized;
      try {
        normalized = normalizeQuery(queryInput.value);
      } catch (_error) {
        hideResult();
        helper.textContent = copyText(locale, "inputInvalid");
        helper.classList.add("error");
        return;
      }
      queryInput.value = normalized.query;
      hideResult();
      helper.classList.remove("error");

      if (activeController) {
        activeController.abort();
      }
      activeRequest += 1;
      const requestNumber = activeRequest;
      const controller = typeof root.AbortController === "function"
        ? new root.AbortController()
        : { signal: undefined, abort() {} };
      activeController = controller;
      let timedOut = false;
      const timeoutId = root.setTimeout(function () {
        timedOut = true;
        controller.abort();
      }, REQUEST_TIMEOUT_MS);

      setLoading(true);
      result.setAttribute("aria-busy", "true");
      helper.textContent = copyText(locale, "finding");
      try {
        const resolved = await resolveAndVerify(
          normalized.query,
          SEARCH_SOURCE,
          controller.signal
        );
        if (requestNumber !== activeRequest) {
          return;
        }
        showDrama(resolved, normalized);
      } catch (error) {
        if (requestNumber !== activeRequest) {
          return;
        }
        hideResult();
        helper.classList.add("error");
        if (error && error.status === 404) {
          helper.textContent = copyText(locale, "notFound");
        } else if (error && error.status === 429) {
          helper.textContent = copyText(locale, "tooMany");
        } else if (timedOut) {
          helper.textContent = copyText(locale, "searchTimeout");
        } else if (error && error.name === "AbortError") {
          return;
        } else {
          helper.textContent = copyText(locale, "searchUnavailable");
        }
      } finally {
        root.clearTimeout(timeoutId);
        if (requestNumber === activeRequest) {
          setLoading(false);
          result.removeAttribute("aria-busy");
          activeController = null;
        }
      }
    }

    searchForm.addEventListener("submit", function (event) {
      event.preventDefault();
      prepareDrama();
    });
    queryInput.addEventListener("input", function () {
      activeRequest += 1;
      if (activeController) {
        activeController.abort();
        activeController = null;
      }
      setLoading(false);
      hideResult();
      helper.classList.remove("error");
      helper.textContent = copyText(locale, "helperIdle");
    });
    queryInput.addEventListener("blur", function () {
      const raw = queryInput.value.trim();
      if (/^[A-Za-z0-9]{4}$/.test(raw)) {
        queryInput.value = raw.toUpperCase();
      }
    });
    continueLink.addEventListener("click", function () {
      if (continueLink.hasAttribute("href")) {
        continueText.textContent = copyText(locale, "opening");
      }
    });

    stories.addEventListener("click", async function (event) {
      const card = event.target.closest
        ? event.target.closest("a.story-link[data-content-id]")
        : null;
      if (!card || !stories.contains(card)) {
        return;
      }
      event.preventDefault();
      if (card.dataset.opening === "true") {
        return;
      }
      if (featuredController) {
        featuredController.abort();
      }
      const controller = typeof root.AbortController === "function"
        ? new root.AbortController()
        : { signal: undefined, abort() {} };
      featuredController = controller;
      card.dataset.opening = "true";
      card.setAttribute("aria-busy", "true");
      featuredNote.textContent = copyText(locale, "checking");
      let timedOut = false;
      const timeoutId = root.setTimeout(function () {
        timedOut = true;
        controller.abort();
      }, REQUEST_TIMEOUT_MS);
      try {
        const resolved = await resolveAndVerify(
          card.dataset.contentId,
          FEATURED_SOURCE,
          controller.signal
        );
        root.location.assign(resolved.route.target_url);
      } catch (error) {
        if (
          error &&
          error.name === "AbortError" &&
          featuredController !== controller
        ) {
          return;
        }
        featuredNote.textContent = error && error.status === 404
          ? copyText(locale, "storyUnavailable")
          : timedOut
            ? copyText(locale, "storyCheckTimeout")
            : copyText(locale, "retry");
      } finally {
        root.clearTimeout(timeoutId);
        card.removeAttribute("aria-busy");
        delete card.dataset.opening;
        if (featuredController === controller) {
          featuredController = null;
        }
      }
    });

    renderFeaturedStories(stories, buildFallbackFeatured(locale), locale);
    carousel.refresh();
    loadFeaturedStories(
      stories,
      featuredTitle,
      featuredNote,
      carousel,
      locale,
      browserLanguages
    );
  }

  const api = Object.freeze({
    CODE_RESOLVER_PATH,
    FEATURED_PATH,
    TARGET_ORIGIN,
    TARGET_PATH,
    SEARCH_SOURCE,
    FEATURED_SOURCE,
    REQUEST_TIMEOUT_MS,
    FEATURED_TIMEOUT_MS,
    FEATURED_MAX_STALE_MS,
    FEATURED_MAX_FUTURE_SKEW_MS,
    DRAG_THRESHOLD_PX,
    MAX_FEATURED_LANGUAGE_BUCKETS,
    TARGET_PARAM_KEYS,
    COPY,
    normalizeLanguageTag,
    localeCandidates,
    getBrowserLanguages,
    resolveUiLocale,
    rankingLanguageCandidates,
    copyText,
    buildFallbackFeatured,
    normalizeQuery,
    requireContentId,
    normalizeSource,
    buildCodeResolverUrl,
    buildFeaturedUrl,
    validateTargetUrl,
    normalizeCodeResolvePayload,
    normalizeDramaPayload,
    isSafeFeaturedCover,
    shanghaiYesterday,
    normalizeFeaturedPayload,
    normalizeFeaturedBundle,
    createDragTracker,
    getCarouselStep
  });

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.TTDramaCodeBridge = api;

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initPage, { once: true });
    } else {
      initPage();
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
