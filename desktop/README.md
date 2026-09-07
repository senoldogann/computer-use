# computeruse Desktop — Aşama 3 (gerçek ajan orkestrasyonu)

Saf SwiftUI + AppKit ile yazılmış yerel macOS arayüzü. Harici bağımlılık yok.

## Gereksinim

- macOS 14.0+ (Sonoma), macOS 15 (Sequoia) uyumlu
- Xcode 15+ / Swift 5.9+

## Çalıştırma

```bash
cd desktop
./scripts/package-desktop.sh   # derler, .app paketler, "ComputerUse Dev" ile imzalar
open ComputerUseDesktop.app
```

> **İmza zorunluluğu:** TCC (Ekran Kaydı izni) izni imza kimliğine bağlıdır.
> İmzasız/ad-hoc binary her derlemede yeni kimlik alır ve izni sessizce
> kaybeder. `package-desktop.sh` her zaman `ComputerUse Dev` sertifikasıyla
> imzalar (`scripts/make_signing_cert.sh` ile bir kez kurulur), böylece bir
> kez verilen izin tüm gelecek derlemelerde korunur.

Xcode ile açmak için `desktop/Package.swift` dosyasını sürükleyip bırakın
(Xcode Swift Package'ı doğal olarak açar; ayrı `.xcodeproj` gerekmez).

## Yapı

```
Sources/ComputerUseDesktop/
├── ComputerUseDesktopApp.swift  # @main giriş, koyu başlık çubuğu + MenuBarExtra + kısayollar
├── ContentView.swift            # 3 panelli düzen + sheet'ler + UI niyet dinleyicileri
├── SidebarView.swift            # 240px: yeni görev + arama + thread listesi + canlı alt ikonlar
├── CenterStageView.swift        # Boş durum (hero) / aktif sohbet (timeline + alta sabit composer)
├── TimelineView.swift           # Dikey zaman çizelgesi: kullanıcı / düşünce / plan / eylem / konsol
├── ComposerView.swift           # Composer kartı (odak + Cmd+Enter + sürücü rozeti)
├── Sheets.swift                 # Ayarlar / İstatistikler / Model paleti (Cmd+K)
├── RightPanelView.swift         # 320px: canlı vizör + reticle + HUD
├── StreamFrameView.swift        # FrameStore + layer-backed kare sunumu
├── CaptureService.swift         # SCStream 60fps + izin akışı + ayna koruması
├── AgentBridge.swift            # Sürücü JSON-RPC + sensörler + iç sürücü SIGINT hedefi
├── AgentRunner.swift            # Gerçek CLI Process + @@CU akış ayrıştırma + SIGINT durdurma
├── AppState.swift               # Paylaşımlı durum + sohbet yaşam döngüsü + kalıcı sayaçlar
├── Models.swift                 # Thread / timeline tipleri + model kataloğu + örnek veri
├── Theme.swift                  # T3 Synara renk paleti
└── VisualEffectView.swift       # NSVisualEffectView köprüsü
```

## Aşama 3 davranışları (doğrulandı — `docs/desktop-stage3.png`)

- Boş durumda hero + ortalanmış composer + tıklanabilir örnek çipler
  (çip composer'a yazar ve odaklar); gönderimde hero kaybolur, kullanıcı
  balonu üstte, altında düşünce/plan/eylem/konsol zaman çizelgesi akar,
  composer alta sabitlenir.
- Gönder (`Cmd+Enter` veya düğme) gerçek
  `python3 -m computeruse --goal … --model openai:<id> --level N --driver … --socket …`
  sürecini başlatır; stdout'taki `@@CU <json>` adım satırları timeline'a,
  stderr/log satırları konsola akar. `--yes` asla geçilmez; `--real`
  yalnızca Ayarlar'daki bilinçli anahtarla eklenir.
- `Acil Durdur` CLI sürecine SIGINT gönderir, run'ı park eder (kullanıcı
  durdurması kuyruğu asla otomatik başlatmaz); park edilen thread kırmızı
  değil boşta (idle) görünür.
- Sol panel: thread seçimi geçmişi yükler, `+` boş duruma döner, alt
  ikonlar gerçek Ayarlar (API key rozeti, model, TCC, sürücü modu) ve
  İstatistik (oturum, gecikme, fps, donanım) sheet'lerini açar.
- Kısayollar: `Cmd+B` sol çubuk, `Cmd+Option+]` sağ vizör, `Cmd+Enter`
  gönder, `Cmd+K` model paleti, `Escape` kapatır. Menü çubuğundaki
  nişangah ikonu durum + Göster/Gizle + Acil Durdur + Çıkış sunar.
- CLI yorumlayıcı çözümleme sırası: `COMPUTERUSE_CLI` (tam komut
  override'ı) → `COMPUTERUSE_PYTHON` → `$COMPUTERUSE_REPO/.venv/bin/python3`
  → bundle/çalışma dizininden yukarı yürüyerek bulunan repo'nun
  `.venv/bin/python3` → `/usr/bin/python3 -m computeruse` (son çare; modül
  yoksa konsola düzeltme ipucu yazılır); `PYTHONUNBUFFERED=1` ile akış
  garantilenir.

## Aşama 2 davranışları (doğrulandı)

- İlk açılışta `CGPreflightScreenCaptureAccess` kontrolü, yoksa
  `CGRequestScreenCaptureAccess` isteği; izin sonrası uygulamaya dönüşte
  yayın otomatik başlar (yeniden başlatma gerekmez).
- Yayın filtresi kendi pencerelerini PID + bundle ID ile hariç tutar
  (ayna döngüsü yok — `docs/desktop-stage2.png` kanıtı).
- Köprü önceliği: gerçek sürücü soketi (`/tmp/actuation-driver.sock`) >
  iç simüle sürücü (`/tmp/actuation-desktop.sock`, yalnızca yaşam
  döngüsü/SIGINT hedefi) > yerel sensörler. Simüle sürücünün canned
  `focused_window` verisi asla HUD'a girmez.
- `Acil Durdur`: run'ı park eder, iç sürücüye `SIGINT` gönderir.
  Dış (CLI) sürücülere asla sinyal gönderilmez.

## Aşama 3 notları

- `AgentBridge.submit` içindeki plan provası yerine model yürütmesini bağla.
- `AutonomyLevel` adları `security/grants.py` sözlüğüyle eşleşir.
