# Computer Use — Kapsamlı Geliştirme, İnceleme ve İyileştirme Raporu

Bu doküman, projede başından itibaren gerçekleştirilen derinlemesine kod incelemelerini, çözülen sistem sorunlarını, mimari iyileştirmeleri ve kullanıcı arayüzü (UI) dönüşümünü özetlemektedir.

---

## 1. 🔍 Kapsamlı Kod İncelemesi (Deep Code Review & Verification)

Proje, Anayasal **6 Değişmez Kural (6 Immutable Laws)** ve **ADR (Architectural Decision Records)** mimarisi ışığında incelenmiş; 14 kritik/önemli bulgu tespit edilerek kanıtlanmıştır:

* **ADR-1 & SYS-03 (Socket Güvenliği)**: `--real` bayrağının `socket_path` argümanını yutması ve çakışma riski incelendi.
* **Law 5.2 & SYS-01 (Event Tap Watchdog)**: macOS `CGEventTapEnable` fonksiyonunun `TapDisabledByTimeout` durumunda kill-switch'i devre dışı bırakma riski incelendi ve oto-reaktif etkinleştirme kalıbı belirlendi.
* **Law 5.1 & SEC-01 (Destructive Action Sınıflandırması)**: `security/autonomy.py` içinde `CallTool` çağrılarının argümanlarının subject metnine dahil edilmemesi açığı incelendi.
* **ADR-2 & SEC-02 (Batch Action Mark Çözümlemesi)**: Batch eylemlerde aradaki ekran kaymalarında mark ID'lerinin bayatlaması (staleness) riski tespit edildi.
* **Law 2.1 & PERF-01 (Token Bütçesi Harcaması)**: `Observation` nesnesinin geçmişe her adımda eklenmesinin yarattığı token enflasyonu analiz edildi.
* **Law 1.2 & VIS-01 (Set-of-Marks Koordinat Ölçeklemesi)**: Ekran haritası (`ScreenMap`) ile görsel koordinatlar arasındaki Retina/DPI dönüşüm sınırları doğrulandı.

*Tüm bu bulgular [code_review_report.md](code_review_report.md) ve [REVIEW_RESULT.md](REVIEW_RESULT.md) dokümanlarında detaylandırılmıştır.*

---

## 2. 🖥️ Menü Barı Çift Cursor / Halo Temizliği & Süreç Yaşam Döngüsü

* **Sorun**: Menü barında iki ayrı imleç/halo belirmesi ve arka planda yetim kalan süreçlerin çakışması.
* **Çözüm**:
  - Arka planda çalışan yetim süreçler (`actuation-driver`, `actuation-menu`) tespit edilip temizlendi.
  - Tekil süreç ve soket izolasyon yapısı sağlandı.
  - macOS uygulama paketleyici scripti (`scripts/package_app.sh`) çalıştırılarak release modunda kod imzalı temiz `ComputerUse.app` paketi `~/Applications/ComputerUse.app` yoluna kuruldu.

---

## 3. 🛡️ Otonomi Seviyeleri (Autonomy Levels) & UI İzin Kontrolü

* **Mevcut Durum**: Anayasa Kuralı 5.1 (Permission Governance) gereği sistem **Guarded Mode (Level 2)** ve **Full Autonomy (Level 3)** seviyelerine sahiptir. Varsayılan olarak Level 2'de çalıştığı için rutin eylemlerde dahi kullanıcı onayı beklemekteydi.
* **Geliştirme**:
  - Arayüzün compose alanına ve native macOS menü barına dinamik bir **Otonomi Geçiş Butonu** eklendi.
  - **Full Auto (Level 3)**: Rutin adımlarda kullanıcıya sormadan sonuna kadar otonom ilerler.
  - **Guarded (Level 2)**: Kritik ve durum değiştiren adımlarda kullanıcıdan onay (`Approve / Deny`) ister.
  - Tercih `localStorage` ve Rust driver atomik durumunda kalıcı olarak saklanacak şekilde entegre edildi.

---

## 4. 🎯 Akıllı Hedef Uygulama (Target App) Otomasyonu

* **Durum**: Kullanıcının komut verirken hedef uygulama belirtme zorunluluğu kaldırıldı.
* **Çalışma Prensibi**:
  - Ajan çalışmaya başladığında macOS Accessibility (AX) ve Quartz pencere yöneticisi ile ekranın en önündeki aktif uygulamayı (`focused_window` & `ax_elements`) otomatik olarak keşfeder.
  - İstenildiğinde belirli bir uygulamayı öne getirmek için UI'a isteğe bağlı `+ App` açılır kutucuğu entegre edildi.

---

## 5. 🔌 MCP (Model Context Protocol) Sistemi & Mağazası (Storefront)

* **Backend Motoru (`src/computeruse/mcp/catalog.py`)**:
  - 12 adet popüler ve doğrulanmış MCP sunucusu entegre edildi:
    1. *Local Filesystem* (Dosya sistemi yönetimi)
    2. *Knowledge Graph Memory* (Kalıcı hafıza grafiği)
    3. *Brave Search* (Canlı web araması)
    4. *GitHub* (Depo, PR ve commit yönetimi)
    5. *Web Fetch* (Sayfa içeriği çekme)
    6. *Notion* (Notion veritabanı ve sayfa yönetimi)
    7. *Slack* (Kanal ve mesaj yönetimi)
    8. *Linear* (Görev ve döngü takibi)
    9. *SQLite Database* (Yerel SQL veritabanı sorgulama)
    10. *PostgreSQL* (İlişkisel veritabanı yönetimi)
    11. *Git Tools* (Yerel git deposu araçları)
    12. *Puppeteer Browser* (Headless tarayıcı otomasyonu)
  - `~/.computeruse/mcp_servers.json` dosyasına yazma, okuma, silme ve yapılandırma fonksiyonları yazıldı.
* **Rust Driver IPC Entegrasyonu**:
  - Rust micro-driver'a `get_mcp`, `save_mcp`, `delete_mcp`, `set_mcp_enabled` komutları eklendi.
  - Ajan çalıştırılırken eklentileri `--mcp` parametresiyle modele aktaran hat bağlandı.
* **UI MCP Mağazası (Eklentiler Sekmesi)**:
  - Tek tıkla eklenti arama, filtreleme, kurma ve kaldırma yeteneği.
  - Parametre/API anahtarı gerektiren eklentiler için form modali.

---

## 6. 📍 Claude Stili Floating Dock (Ekranın Alt Ortası) Konumu

* **Konumlandırma**: Rust AppKit koduna `position_bottom_center()` fonksiyonu eklendi; panelin ekranın alt ortasında, macOS Dock'unun 20pt üzerinde süzülmesi (`NSScreen::visibleFrame`) sağlandı.
* **Esnek Geçiş**: Hem arayüzdeki hap butondan hem de menü barı sağ tık menüsünden tek tıkla **Dock Üstü (Claude Stili)** ile **Menü Barı (Sağ Üst)** arasında geçiş yapabilme yeteneği eklendi.

---

## 7. 🎨 Claude Anthropic Resmi Teması & %100 Saf SVG İkon Dönüşümü

### A. Claude Resmi Renk Paleti (Design Tokens)
* `--accent`: `#D97757` (Resmi Anthropic Terakota Turuncusu)
* `--accent-hover`: `#E08365`
* `--accent-glow`: `rgba(217, 119, 87, 0.35)`
* `--bg-base`: `#1A1916` (Sıcak Claude Koyu Tuval)
* `--surface`: `#22211D` (Panel Yüzeyi ve Kartlar)
* `--surface-hover`: `#2B2A25`
* `--border`: `#33312B` / `--border-highlight`: `#45433B`
* `--text-primary`: `#FAF9F5` (Claude Fildişi Krem / Kağıt Beyazı)
* `--text-secondary`: `#B0AEA5` (Sıcak Orta Gri)
* `--text-tertiary`: `#75736B` (Pasif Gri)
* `--success`: `#788C5D` (Zeytin Yeşili)
* `--warning`: `#D4A359` (Kehribar Sarısı)
* `--error`: `#D05548` (Mercan Kırmızısı)
* `--cyan`: `#6A9BCC` (Gökyüzü Mavisi)
* `--purple`: `#9D79BC` (Leylak Moru)

### B. Sıfır Emoji Kuralı (%100 Pure Inline SVG)
Arayüzdeki tüm emojiler kaldırıldı; yerlerine yüksek çözünürlüklü vektör SVG ikonlar entegre edildi:
* **Marka Logosu**: Anthropic Claude **Radiant Spark (Güneş Işını / Yıldız)** SVG.
* **Sayaçlar**: Token sayacı için SVG Şimşek, geçen süre için SVG Saat.
* **Sekmeler**: `Live Agent`, `Eklentiler`, `History`, `Analytics` saf SVG ikonlarla donatıldı.
* **Fiziksel Eylemler**: Fare tıklaması, metin yazımı, pano yapıştırması, kısayol tuşları ve uygulama pencereleri için SVG ikonlar.
* **Kontrol Butonları**: Full Auto (SVG Şimşek), Guarded (SVG Kalkan), MCP (SVG Fiş), Dock (SVG Ekran), Run Agent (SVG Gönder Oku + `⌘⏎`).
* **MCP Mağazası**: Arama büyüteci, artı (`+`), onay (`✓`), silme (`✕`) saf SVG olarak işlendi.

---

## 8. 🧪 Test, Doğrulama ve Paketleme Durumu

| Bileşen | Çalıştırılan Doğrulama | Durum (4 Eylül 2026, ölçüldü) |
|---|---|---|
| **Rust Micro-Driver** | `cargo test` | **61 passed + 1 ignored** |
| **Rust Linter** | `cargo clippy --all-targets -- -D warnings` | **0 Uyarı** |
| **Python Backend** | `pytest tests/` | **722 / 722 Test Başarılı** |
| **Öz-değerlendirme Bataryası** | `computeruse --eval` | **12 / 12 passed** |
| **Python Linter** | `ruff check src/ tests/` | **0 Hata / 0 Uyarı** |
| **Python Tip Denetimi** | `pyright` | **0 Hata / 0 Uyarı** |
| **macOS Paketi** | `scripts/package_app.sh` | **Kuruldu (`~/Applications/ComputerUse.app`)** |
| **Çalışma Durumu** | macOS LaunchServices | **Aktif** |

---

## 9. ⚡ Arama Döngü Koruması & Tavily / Exa MCP Entegrasyonu

* **Kullanıcı Geri Bildirimi**: Ajanın `web_search` boş sonuç döndüğünde 22 adım boyunca sürekli arama yapıp Notlar'a geçememesi incelendi.
* **Akıllı Fallback & Koruma Entegrasyonu (`loop.py`)**:
  - `web_search` ardışık sonuç alamadığında veya hata verdiğinde ajana tam olarak kullanıcının belirttiği akıllı yönlendirme enjekte edildi:
    > *"web_search sonuç döndüremedi. Aramayı web_search ile tekrarlamayın! Eğer kurulu başka bir web arama MCP'si varsa (`call_tool`) onu deneyin; yoksa doğrudan ekrandaki Google Chrome tarayıcısına geçin ve arama çubuğunu kullanın."*
  - 2 ardışık başarısızlıktan sonra `web_search` çağrıları kilitlenerek ajanın sonsuz döngüye girmesi engellendi ve fiziksel tarayıcıya (Chrome) geçmesi zorunlu kılındı.
* **Tavily ve Exa MCP Sunucuları Kataloğa Eklendi**:
  - **Tavily AI Search (`tavily-mcp@latest`)**: LLM'ler için optimize edilmiş yüksek hızlı, CAPTCHA'sız gerçek zamanlı web arama motoru (`TAVILY_API_KEY` ile).
  - **Exa Neural Search (`exa-mcp-server`)**: Semantik ve nöral web araması, web kazıma ve benzer sayfa keşif aracı (`EXA_API_KEY` ile).
  - Her iki sunucu da hem Python `CURATED_CATALOG` (`src/computeruse/mcp/catalog.py`) hem de UI MCP Mağazasına (`menu.html`) kuruluma hazır şekilde entegre edildi.

---
*Doküman Güncelleme Tarihi: 4 Eylül 2026*

## 10. 🎯 P2: İkon ve Glif Körlüğünün Çözümü (Set-of-Marks & AX Zenginleştirmesi)

* **Sorun**: Menü, arama, sekme kapatma gibi metinsiz simgelerde modeller koordinat tahminine kaçıyor ve çözünürlük ölçek farkları nedeniyle tıklamaları ıskalayabiliyordu.
* **Rust Driver Çözümü (`driver/src/ax.rs`)**:
  - `AXTitle` ve `AXDescription` boş olduğunda `AXHelp` (araç ipucu) ve `AXRoleDescription` nitelikleri okunacak şekilde hiyerarşi genişletildi.
  - Safari kenar çubuğu ("Show Sidebar"), sekme kapatma ("Close Tab") ve benzeri ikonlar otomatik olarak anlamlı isimlere kavuştu.
  - `test_ax_help_and_description_fallback` birim testi ile doğrulandı (61 test).
* **Model İstemi & Normalizasyon (`prompts.py`)**:
  - `ACTION_CONTRACT` içindeki `SUPPORTED ACTIONS` listesine `click_mark: {"type": "click_mark", "mark": int}` eklendi.
  - `VISUAL GROUNDING` bölümünde başlıksız ikon butonlarında dahi yeşil kutunun hedef ikonu sarmalaması durumunda doğrudan `[mark]` ile tıklanması kuralı zorunlu kılındı.
  - LLM modellerinin ürettiği `mark` ve `click_element` biçimleri otomatik olarak `click_mark` formatına normalize edildi.
* **Testler**:
  - `test_system_prompt_documents_click_mark` (istem dokümantasyonu doğrulaması)
  - `test_normalize_action_dict_normalizes_click_mark_aliases` (aksiyon normalizasyonu)
  - `test_click_mark_resolution_on_untitled_icon_elements` (başlıksız ikonların mark merkezine çözünmesi)
