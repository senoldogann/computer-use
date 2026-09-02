# Derinlemesine Kod İnceleme Raporu — computeruse

**Tarih:** 2026-09-01
**Kapsam:** Tüm repository (Python orkestrasyon katmanı, Rust actuation driver'ı, WKWebView panel, testler, CI, dokümantasyon) — dosya dosya, satır satır.
**Yöntem:** Statik inceleme + tüm doğrulama kapılarının (typecheck/lint/test/build) yerel çalıştırılması + sürüm-hassas iddiaların web aramasıyla doğrulanması (GPT-5.6 fiyatlandırması, core-graphics API imzaları, crate sürümleri).

---

## 0. Yönetici Özeti

| Bulgu | Şiddet | Konum |
|---|---|---|
| **H1** — macOS'ta kaydırma (scroll) eksenleri takaslı: `dx` dikey, `dy` yatay olarak iletilir | **Yüksek** | `driver/src/quartz.rs:324` |
| **H2** — `SessionCheckpoint.completed_steps_count` her zaman `0` yazılır (`"runner" in locals()` kapama değişkenini asla göremez) | **Orta** | `src/computeruse/agent.py:315-322` |
| **M1** — `last_error`, başarılı kurtarma sonrası temizlenmiyor (yorum kodu yalanlıyor) | Orta | `src/computeruse/orchestrator/loop.py:688-693` |
| **M2** — `Risk.ROUTINE` ve ~30 dilli `_ROUTINE_MARKERS` ölü politika: hiçbir seviye ROUTINE'i CONFIRM'e eşlemez | Orta | `src/computeruse/security/autonomy.py:235-274` |
| **M3** — `open_tabs` her state geçişinde düşürülüyor (yalnızca `_observe` geri yüklüyor) | Orta-düşük | `loop.py:353-365, 649, 728` |
| **M4** — `verify_capture_region` kırpmadan ÖNCE tüm kareyi saf-Python luma'ya çeviriyor (gerçek Retina ekranda saniyeler) | Orta | `src/computeruse/vision/capture.py:152-153` |
| **M5** — `ensure_single_instance` PID dosyasını geri dönüşüme karşı korumasız; stale PID başka bir süreci öldürebilir | Orta-düşük | `driver/src/menu.rs:98-119` |
| **M6** — Launcher `--level 3` ile çalışıyor ama panelden onay kanalı yok → yıkıcı eylemler her zaman sert hata verir | Orta-düşük | `driver/src/menu.rs:534` |
| **M7** — `openai.py` fiyatlandırma yorumları güncel değil (2026-07-30 fiyat indirimi sonrası) | Düşük | `src/computeruse/providers/openai.py:9-17` |
| **M8** — CI yalnızca Ubuntu; macOS-gated Rust modülleri (quartz/ax/hotkey/indicator/menu ≈ 2.900 satır) hiç derlenmiyor, clippy görmüyor | **Orta (süreç)** | `.github/workflows/ci.yml:18,59` |
| **L1-L13** — Dokümantasyon sapmaları, küçük hatalar, ölü kod | Düşük | Tablo 4 |

**Genel değerlendirme:** Mimari tutarlı ve konstitüsyon (AGENTS.md) ile uyumlu; test altyapısı olağanüstü (246 Python testi + 24 Rust testi, hermetic, sözleşme-sapma testleri). Yerel kapıların tamamı yeşil. Bulguların çoğu *gerçek macOS donanımında koşulduğunda* ortaya çıkacak cinsten — çünkü CI macOS modüllerini hiç derlemiyor. H1 (scroll ekseni) ve H2 (checkpoint) en önce düzeltilmesi gereken gerçek hatalardır.

---

## 1. Doğrulama Metodolojisi ve Kanıtlar

### 1.1 Yerel olarak çalıştırılan kapılar (hepsi geçti)

| Kapı | Komut | Sonuç |
|---|---|---|
| Tip kontrolü | `uv run pyright` | `0 errors, 0 warnings, 0 informations` |
| Lint | `uv run ruff check .` | `All checks passed!` |
| Python testleri | `uv run pytest -q` | `246 passed in 7.45s` |
| Rust testleri | `cargo test --all-targets` | `24 passed; 0 failed` |
| Clippy | `cargo clippy --all-targets -- -D warnings` | Temiz |

Not: Bu makine macOS (arm64) olduğundan, Rust tarafında macOS-gated modüller de gerçekten derlendi — CI'nin göremediği kapsam burada yerel olarak doğrulandı.

### 1.2 Web ile doğrulanan sürüm-hassas iddialar

- **GPT-5.6 model ailesi gerçek:** OpenAI, Sol/Terra/Luna üçlüsünü 2026 yılında yayınladı (openai.com ve openrouter.ai doğrulaması). Ancak **30 Temmuz 2026 tarihli fiyat indirimi** ile Terra $2.50/$15 → **$2/$12**, Luna $1/$6 → **$0.20/$1.20** oldu; `openai.py` dokümantasyonu indirim öncesi rakamları yazıyor.
- **core-graphics 0.25.0 `new_scroll_event` imzası** kaynak koddan doğrulandı: `(source, units, wheel_count, wheel1, wheel2, wheel3)` — **wheel1 dikey eksendir**. `quartz.rs` `wheel1=dx, wheel2=dy` geçtiği için H1 kesindir.
- **Kilitli crate sürümleri:** core-graphics 0.25.0, core-foundation 0.10.1, objc2 0.6.4, objc2-app-kit 0.3.2, objc2-web-kit 0.3.2, block2 0.6.2, serde 1.0.229, serde_json 1.0.151 (Cargo.lock). Python tarafı: **pydantic 2.13.5** (pyproject `>=2.7,<3.0` ile uyumlu).

### 1.3 Kapsam dışı / not

- Repo `data/` dizini (CCM generation cache) `.gitignore` ile hariç tutulmuş; incelenmedi.
- `driver/target/` derleme çıktısı; incelenmedi.

---

## 2. Mimari Değerlendirme (Olumlu Bulgular)

1. **ADR-1 ayrımı gerçek:** Rust driver'ı ayrı süreç, Unix soketi üzerinden JSON-RPC; Python tarafı asla `import` etmiyor. `client.py`'nin tamponlu okuma mantığı (split/pipelined yanıtlar, F3) testle sabitlenmiş.
2. **OODA döngüsü gerçek bir "fonksiyonel çekirdek + imperatif kabuk" ayrımı:** `decide_step`/`target_point_of`/`verification_region`/`verdict` saf; `OodaRunner` yan etkileri sahipleniyor. F2 (başarısız adım completed_steps'e girmez) testle korunuyor.
3. **Güvenlik duruşu sağlam:** soket `0o600`, API anahtarı yalnızca env/`~/.computeruse/env`, simüle backend varsayılan, `--real` izinsiz host'a dokunmuyor, kill-switch kanalları (Ctrl-C, küresel hotkey, mouse shake) birleştirilmiş ve G2 çakışma koruması var.
4. **Testler** (246+24) hermetic ve niyetli: `test_contract_drift.py` Python ↔ Rust tel sözleşmesini gerçek driver üzerinden sabitliyor; `test_audit_v2.py`/`test_regressions.py` geçmiş bulguları (G1-G7, F1-F4) geri dönüşe karşı pinliyor.
5. **`AGENT_INSTRUCTIONS_FIX.md`'deki 6 düzeltmenin 6'sı da uygulanmış ve testli:** ClipboardPaste risk sınıflandırması (`autonomy.py:222`), fenced-JSON regex (`prompts.py`), exception sonrası `screenshot_b64` korunması (`loop.py:649`), kompozit hotkey normalizasyonu (`test_parse_normalizes_aliases`), `clearAllHistory`'de `window.confirm`'un kaldırılması (`menu.html:1729`).
6. Kodda hiç `TODO`/`FIXME`/`pytest.mark.skip`/`xfail` yok.

---

## 3. Bulgular — Kanıtlı

### H1 (YÜKSEK) — Scroll eksenleri takaslı: dikey istek yatay kaydırma üretir

**Konum:** `driver/src/quartz.rs:324`

```rust
let event = CGEvent::new_scroll_event(self.source.clone(), ScrollEventUnit::PIXEL, 2, dx as i32, dy as i32, 0)
```

**Kanıt:** core-graphics 0.25.0 kaynağında imza `(source, units, wheel_count, wheel1, wheel2, wheel3)` ve Apple'ın `CGEventCreateScrollWheelEvent2` sözleşmesinde **wheel1 = dikey eksen, wheel2 = yatay eksen**. Oysa sözleşme ve prompt `dy`'yi dikey tanımlıyor:

- `schemas.py` `MouseScroll`: `dx: int; dy: int`
- `prompts.py` §6: *"To scroll DOWN … mouse_scroll with dy=positive (e.g. {"dy": 300})"*

Dolayısıyla gerçek macOS'ta `dy=300` isteği **wheel1=0 (dikey kaydırma yok) + wheel2=300 (yatay kaydırma)** üretir. ORIENT doğrulaması da bu hatayı yakalayamaz çünkü simüle backend yalnızca loglar ve `verify_capture_region` "değişiklik yok" der.

**Öneri:** `wheel1=dy, wheel2=dx` (işaret yönü ayrıca gerçek donanımda doğrulanmalı). Rust tarafına saf bir birim testi + `SimulatedBackend`'in çağrıyı doğrulaması eklenmeli.

---

### H2 (ORTA) — Checkpoint `completed_steps_count` her zaman 0

**Konum:** `src/computeruse/agent.py:315-322`

```python
def _on_sub_goal_complete(current_plan: GoalPlan) -> None:
    steps_count = len(runner.executed_trajectory) if "runner" in locals() else 0
    SessionCheckpoint(..., completed_steps_count=steps_count, ...).save(checkpoint_dir)
```

**Kanıt:** `runner` dış (closure) değişkendir; Python'da `locals()` kapama değişkenlerini **içermez** (basit repro ile doğrulandı: `"runner" in locals()` → `False`). Bu nedenle `steps_count` her zaman 0'dır; `--plan` ile yazılan her checkpoint "0 tamamlanmış adım" kaydeder. Niyet `runner` tanımsızken NameError'dan kaçınmaktı; ama callback yalnızca `runner.run()` içinden çağrıldığı için `runner` her zaman tanımlıdır.

**Öneri:** Guard'ı kaldırıp doğrudan `len(runner.executed_trajectory)` kullanın (ya da `nonlocal` ile geçirin). Davranışı sabitleyen bir test ekleyin.

---

### M1 (ORTA) — `last_error`, başarılı kurtarmadan sonra temizlenmiyor; yorum kodla çelişiyor

**Konum:** `src/computeruse/orchestrator/loop.py:688-693`

```python
# A successful action clears obsolete recovery diagnostics. Preserve
# the most recent failure only when this action itself emitted a
# fresh repetition hint; otherwise a verified success restores a
# clean working context.
state = WorkingState(
    ...
    last_error=repeat_hint if repeat_hint is not None else state.last_error,
```

**Kanıt:** Kod, yeni bir tekrarlama ipucu yoksa `state.last_error`'ı (önceki hatayı) koruyor; yorum "temiz bağlam" vaat ediyor. Sonuç: bir görsel doğrulama hatasından sonra model kurtarılıp doğru eylemi yapsa bile, hata metni sonraki turlarda da context'te kalır ve modeli yanlış yönlendirebilir. Mevcut testler bu davranışı pinlemiyor.

**Öneri:** Yorumla uyumlu hale getirin: `last_error=None` (yeni tekrarlama ipucu yoksa). Davranışı testleyin.

---

### M2 (ORTA) — `Risk.ROUTINE` ve `_ROUTINE_MARKERS` ölü politika

**Konum:** `src/computeruse/security/autonomy.py:235-274`

```python
if words & self.routine_markers:
    return Risk.ROUTINE          # satır 239 — sınıflandırma ROUTINE döndürebiliyor
...
def decide_permission(level, risk):
    if risk is Risk.DESTRUCTIVE:   # 261
        ...
    if level is AutonomyLevel.FULL:
        return PermissionDecision.ALLOW   # 267
    if level is AutonomyLevel.OBSERVER:
        return PermissionDecision.BLOCK
    if level is AutonomyLevel.SUPERVISED:
        return PermissionDecision.CONFIRM
    return PermissionDecision.ALLOW        # 274 — GUARDED (varsayılan seviye!)
```

**Kanıt:** `Risk.ROUTINE` hiçbir dalda CONFIRM'e eşlenmiyor. Varsayılan seviye GUARDED'da (CLI varsayılanı `--level 2`) "confirm dialog", "save", "close" gibi `_ROUTINE_MARKERS` içeren eylemler **otomatik ALLOW** alır — oysa `_ROUTINE_MARKERS` docstring'i *"worth a confirmation in guarded mode"* diyor. `decide_permission`'a `Risk.ROUTINE → CONFIRM (GUARDED)` dalı eklenmedikçe ~30 dillik rutin işaretçi listesi hiçbir etki üretmiyor.

**Öneri:** Ya GUARDED + ROUTINE → CONFIRM eşlemesini ekleyin (docstring ile uyum) ya da ölü listeyi ve `Risk.ROUTINE`'ı kaldırın. İki durumda da test (`decide_permission(GUARDED, ROUTINE)`) ekleyin.

---

### M3 (ORTA-DÜŞÜK) — `open_tabs` state geçişlerinde düşüyor

**Konum:** `src/computeruse/orchestrator/loop.py:353-365` (`decide_step`), `:649` (exception kurtarma), `:728` (success), `_retrieve` state rebuild'i, blind-warning rebuild'i.

**Kanıt:** `WorkingState`'i yeniden kuran **tüm** kod yolları `open_tabs`'ı iletmiyor (varsayılan `()`). Yalnızca `_observe` (satır 879) onu yeniden dolduruyor. `_observe`'in kendi iddiası *"the state never flickers through a half-refreshed intermediate"* — ama her karar/hatadan sonra sekmeler `()`'a düşüyor ve bir sonraki turda AX probe'u başarısız olursa (ör. consent kaybı) kayboluyor. Ayrıca `prompts.py` her turda "Open browser tabs" bölümünü `state.open_tabs` üzerinden render ettiği için, karar turunda sekmeler yalnızca `_observe` sonrası state'te görünüyor — düşüş pratikte tutarsızlık yaratıyor.

**Öneri:** `decide_step` ve tüm rebuild'lere `open_tabs=state.open_tabs` ekleyin; bir regresyon testi pinleyin.

---

### M4 (ORTA) — Görsel doğrulama, kırpmadan önce tüm kareyi saf-Python decode ediyor

**Konum:** `src/computeruse/vision/capture.py:126-153`

```python
before_region = crop_luma(to_luma_grid(before), scaled_region)
after_region = crop_luma(to_luma_grid(after), scaled_region)
```

**Kanıt:** `to_luma_grid` (satır 105) her piksel için Python döngüsüyle çalışıyor (`for y … for x …`). 2x Retina 3024×1964 karede ≈ 6M piksel × 2 kare × her doğrulama adımı. Kırpma (48pt bölge ≈ 96×96 px) aslında iki tam kare decode'undan sonra yapılıyor. `_downscale_integer`'ın docstring'inde iddia edilen 30ms yalnızca o fonksiyon için geçerli; luma decode'u bu kadar hızlı değil. Gerçek macOS'ta her ORIENT adımı saniyeler sürebilir. Simüle testler (64×36) bunu asla açığa çıkarmaz.

**Öneri:** Önce kırp (byte düzeyinde, bölge piksel aralığını hesaplayarak) sonra decode et; veya `to_luma_grid`'i memoryview/vectorized hale getir. Performansı bir mikro-benchmark testiyle sabitleyin.

---

### M5 (ORTA-DÜŞÜK) — PID dosyası geri dönüşümüne karşı korumasız tek-instance kontrolü

**Konum:** `driver/src/menu.rs:98-119`

```rust
let pid_file = "/tmp/actuation-menu.pid";
if let Ok(content) = std::fs::read_to_string(pid_file) {
    if let Ok(old_pid) = content.trim().parse::<i32>() {
        if old_pid != my_pid {
            unsafe { libc::kill(old_pid, libc::SIGTERM); }
```

**Kanıt:** Çökme/`kill -9` sonrası stale bir PID dosyası kalabilir; macOS PID'leri geri dönüştürdüğünde bu, **ilgisiz bir süreci SIGTERM ile öldürür**. Ayrıca bekleme döngüsü 2,5 saniyeyle sınırlı; süreç bu sürede çıkmazsa yine de üzerine yazar (iki instance çalışabilir). Dosya `/tmp` altında olduğundan aynı makinedeki başka bir kullanıcı da içeriğini değiştirebilir (düşük risk, tek kullanıcılı araç).

**Öneri:** PID doğrulamasını süreç adı ile çapraz kontrol edin (`sysctl KERN_PROCARGS` / `ps -p <pid> -o comm=` eşleşmesi), SIGTERM yerine önce durum yoklaması yapın, dosyayı kullanıcıya özel bir dizine taşıyın.

---

### M6 (ORTA-DÜŞÜK) — Launcher tam özerklikte çalışıyor ama onay kanalı yok

**Konum:** `driver/src/menu.rs:534` — `agent_args` her zaman `--level 3` geçiyor.

**Kanıt:** `decide_permission(FULL, DESTRUCTIVE) = CONFIRM`. Launcher'ın spawn ettiği CLI'nin stdin'i TTY değil, bu yüzden `build_config` `confirm_handler=None` verir (`cli.py` — `sys.stdin.isatty()`). Sonuç: panelden başlatılan bir çalışmada yıkıcı eylem (ör. "delete") her zaman `PermissionConfirmationRequired` → "confirmation required" hatasıyla biter; kullanıcının panelden onaylaması imkânsız. Yani paneldeki "Guarded" sözü fiilen "yıkıcı = sert başarısızlık" demek.

**Öneri:** Ya panel JS'ine bir onay akışı ekleyin (bridge üzerinden `cmd:"confirm"`), ya da launcher seviyesini `--level 2` yapıp aynı durumu kabul edin ve panelde açıkça belirtin.

---

### M7 (DÜŞÜK) — `openai.py` fiyatlandırma yorumları güncel değil

**Konum:** `src/computeruse/providers/openai.py:9-17`

> "$2.50/$15 per 1M tokens" (Terra), "gpt-5.6-sol ($5/$30) is 2x the price", "gpt-5.6-luna ($1/$6)"

**Kanıt:** Web doğrulaması (openai.com, openrouter.ai, cloudzero, 2026-07-30 duyurusu): indirim sonrası **Terra $2/$12, Luna $0.20/$1.20**; Sol $5/$30 değişmedi. Varsayılan model seçimi (Terra) hâlâ mantıklı, yalnızca sayılar bayat. "2x" iddiası da artık ~2.5x.

---

### M8 (ORTA — SÜREÇ) — CI, macOS-gated Rust modüllerini hiç derlemiyor

**Konum:** `.github/workflows/ci.yml:18` ve `:59` — her iki job da `ubuntu-latest`.

**Kanıt:** `lib.rs`'te `#[cfg(target_os = "macos")]` ile gated olan `quartz.rs` (606 satır), `ax.rs` (365), `indicator.rs` (364), `menu.rs` (981), `hotkey.rs` (130), `bin/menu.rs` Ubuntu'da derlenmez, test edilmez, clippy görmez. Yani projenin **gerçek donanımla etkileşen tüm kodu CI'nin kör noktasıdır** (H1 bunun kanıtı: scroll hatası CI'da yakalanamazdı). Yerel macOS makinede `cargo test` + clippy temiz geçti; sorun derleme değil, sürekli koruma.

**Öneri:** Rust job'ına (en azından `cargo check` + `cargo clippy` + macOS'ta çalışan testler için) bir `macos-latest` ayağı ekleyin. Maliyet tek runner.

---

## 4. Düşük Şiddet / Dokümantasyon Sapmaları

| # | Bulgu | Kanıt |
|---|---|---|
| L1 | **Aksiyon sayısı bayat:** AGENTS.md §4'te 9 aksiyon listeleniyor; kodda 11 (`clipboard_paste`, `activate_app` eksik). README.md:37 "9 Pydantic action contracts" diyor. | `schemas.py` — 11 model |
| L2 | **ADR-3 tablosu `openai` SDK diyor; gerçekte stdlib `urllib`** kullanılıyor, `openai` bağımlılığı yok. | `pyproject.toml` (yalnız pydantic); `providers/openai.py` |
| L3 | **AGENTS.md §6 ağaç şeması bayat:** `orchestrator/planner.py`, `memory/schemas.py`, `vision/som.py`, `bin/menu.rs` yok; README.md:64 "`tests/(unit/)`" dizini mevcut değil (unit testleri `tests/smoke/` altında). | `find` çıktısı |
| L4 | **"Sea-blue" vs emerald:** AGENTS.md:20, README.md:131-155 "translucent sea-blue halo" der; uygulama emerald `#50A574` (`indicator.rs` `BRAND_GREEN`, `menu.rs` `EMERALD`). `menu.rs` testi bile `menu_icon_is_sea_blue_squircle...` adını taşıyıp (80,165,116) emerald rengini assert ediyor. | `indicator.rs`, `menu.rs:832` test |
| L5 | **Prompt/schema varsayılan uyumsuzluğu:** `ACTION_CONTRACT` "mouse_drag duration default 400", "type_text wpm default 50" diyor; şemalar `duration_ms=200` ve `wpm=40` kullanıyor. Model belirtmediğinde prompt'ta okuduğu değerler geçerli olmaz. | `prompts.py` §2 vs `schemas.py` |
| L6 | **`is_mouse_shake` docstring'inde** "``None``-condition ahead" ifadesi işlevde yok (bayat metin). | `killswitch.py` |
| L7 | **`_verify_finish`** yorumu "Avoid an extra capture here" deyip hemen ardından `window_probe()`/`ax_probe()` çağırıyor (fazladan RPC). | `loop.py:1142-1170` |
| L8 | **`som.py` yalıtılmış modül:** yalnızca testler tarafından import ediliyor, OODA döngüsüne bağlı değil ("Phase 4" iddiası gerçekleşmemiş). Ayrıca "numbered badges" iddiası yanlış — `annotate_set_of_marks` numara **çizmiyor**, yalnızca düz yeşil kare; `parse_ax_elements_to_marks` sessizce 30 elemanla sınırlı (`ui_elements[:30]`) oysa AX tavanı 64. | `som.py:39,126-131` |
| L9 | **Adım başına gereksiz RPC:** `agent.py`'nin `ax_probe`'u her turda tekrar `client.focused_window()` çağırıyor (runner'ın `window_probe`'uyla birlikte aynı bilgi iki kez çekiliyor + `ax_snapshot`). `focused_text_value_probe` de aynı ikiliyi tekrarlıyor. Adım başına 3-5 RPC. | `agent.py:246-262, 266-280` |
| L10 | **`_call_model` `TypeError`'ı maskeliyor:** modelin içindeki gerçek bir `TypeError` "image_b64 desteklemiyor" sanılıp yeniden çağrılıyor. | `prompts.py:426-430` |
| L11 | **Panelde attribute-injection (self-XSS):** geçmiş "Re-run" butonu hedef metnini `onclick` attribute'una `escapeHtml` (yalnız `&<>`) + `'` escape'i ile gömüyor; `"` escape edilmediği için `"` içeren bir hedef attribute dışına çıkıp yeni attribute enjekte edebilir. Veri kullanıcının kendi hedefleri + localStorage olduğundan kendi kendine XSS, ama yine de injection. | `menu.html:1708` |
| L12 | **README "Not yet implemented" bölümü bayat:** README.md:309 "Natural next frontiers: vision input to the model" diyor; oysa multimodal görüntü akışı (`screenshot_b64` + `openai_model` image_url) uygulanmış ve test edilmiş durumda. | `test_openai_model_multimodal_request_shape` |
| L13 | **Test düzeni tutarsız:** `tests/test_prompts.py` (eski, `_normalize_action_payload` odaklı) ile `tests/smoke/test_prompts.py` (yeni, kapsamlı) ayrı ayrı duruyor; ayrıca `AGENTS.md`'de vaat edilen `tests/unit/` yok. | `find` çıktısı |
| L14 | **`_SEMANTIC_KEYS` içinde `duration_ms`:** skill imzasına süre değerleri giriyor; aynı akışın iki koşusu (ör. `wait` 1000ms vs 2000ms) farklı imza üretir — de-dup kırılganlığı. | `distiller.py` `_SEMANTIC_KEYS` |
| L15 | **`agent.py` AX budama notu** "truncated at 64 elements" metnini sabit yazıyor; `AX_MAX_ELEMENTS` değişirse metin desenkron kalır. | `agent.py:258-262` |
| L16 | **`openai.py` görüntü `"detail": "high"`:** zaten mantıksal çözünürlüğe indirilmiş kare için high-detail, tur başına ~1.100 token (512px tile'lama) demek; maliyet optimizasyonu olarak `auto`/`low` değerlendirilebilir (bilinçli takas olabilir). | `openai.py` model_call |

---

## 5. Dosya Dosya İnceleme Özeti

### Python — çekirdek
| Dosya | Satır | Durum / Not |
|---|---|---|
| `agent.py` | 372 | Kompozisyon katmanı temiz; **H2** (checkpoint), L9 (çift RPC), L15 (sabit 64 metni) |
| `cli.py` | 447 | Sağlam hata sınıflandırması; stderr drain thread'de `stderr_tail` listesine main thread'den okuma (join sonrası, düşük risk) |
| `orchestrator/loop.py` | 1374 | En iyi modül; **M1** (last_error), **M3** (open_tabs), L7 (finish probe) dışında tutarlı |
| `orchestrator/schemas.py` | 115 | Katı, discriminated union doğru; L5 varsayılan uyumsuzluğu |
| `orchestrator/prompts.py` | 467 | Çok iyi scaffolding; L10 (TypeError maskeleme), L5 |
| `orchestrator/client.py` | 293 | Tamponlu okuma + backoff doğru; eksiksiz |
| `orchestrator/planner.py` | 193 | Çalışıyor; Türkçe NLP `replace`'leri ("ve", "sonra", "aç", "ara") İngilizce hedef metinlerinde yanlış bölme riski taşıyor (küçük); `--plan` varsayılan kapalı |
| `providers/openai.py` | 153 | Temiz transport; **M7** (bayat fiyat), L2 (SDK vs urllib), L16 |
| `skills/` (schemas/registry/distiller) | 84+118+148 | İki aşamalı retrieval doğru; L14 (duration_ms imzası) |
| `memory/` (schemas/episodic/semantic) | 41+153+174 | Sağlam; `known_signatures` light-read korunuyor (G4) |
| `security/autonomy.py` | 291 | **M2** (ölü ROUTINE) dışında sağlam; çok dilli marker seti |
| `security/killswitch.py` | 195 | G2 koruması iyi; L6 (bayat docstring) |
| `vision/ax.py` | 223 | ADR-2 birincil kaynak; sağlam, iyi testli |
| `vision/capture.py` | 372 | **M4** (decode-before-crop); PNG encoder saf stdlib, doğru |
| `vision/coordinates.py` | 160 | Saf ve doğru |
| `vision/diff.py` | 215 | Anti-aliasing toleransı iyi düşünülmüş; G3 korumalı |
| `vision/focus.py` / `som.py` | 48 / 143 | focus sağlam; som yalıtılmış (L8) |

### Rust — driver
| Dosya | Satır | Durum |
|---|---|---|
| `main.rs` | 292 | Tek iş parçacığı/bağlantı modeli doğru; `0o600` soket; kill-hotkey yalnızca `--real` |
| `protocol.rs` | 217 | Serde enum sözleşmesi Python ile birebir; testli |
| `backend.rs` | 477 | Trait ayrımı + deterministik simüle AX/Safari fixture mükemmel |
| `bezier.rs` | 236 | Saf Bezier + ease-in-out; testler kapsamlı |
| `quartz.rs` | 606 | **H1** (scroll) dışında kaliteli; modifier release, drag kill-switch temizliği iyi düşünülmüş |
| `ax.rs` | 365 | CF referans yönetimi (get/create rule) doğru; window-list fallback akıllıca |
| `hotkey.rs` | 130 | Saf `matches_kill_combo` + tap; testli |
| `indicator.rs` | 364 | Halo/status item; "isilti" sözcüğü §7.3'e göre İngilizce olmayan yorum (önemsiz); L4 renk |
| `menu.rs` | 981 | **M5** (PID), **M6** (level 3); tek-instance dışında sağlam |
| `assets/menu.html` | 1835 | **L11** (attribute injection), L8 numarasız badge; aksi halde kapsamlı UI |

### Testler / CI / Scripts
- `tests/smoke/*` (30 dosya): 246 test, hermetic, niyetli — bu projenin en güçlü yanı.
- `.github/workflows/ci.yml`: **M8** — yalnızca Ubuntu, macOS gated kod kör nokta; pytest başarısızlık çıktısı GITHUB_STEP_SUMMARY'a taşınıyor (iyi).
- `scripts/package_app.sh`: ad-hoc ama tutarlı; `codesign --deep --sign -` (ad-hoc imza) dağıtım için not edilmeli.
- `docs/superpowers/plans/2026-09-01-ci-pipeline.md`: uygulanmış plan; "Expected Test Results: rust 24 / python 246" hedefi birebir tutmuş (24+246).

---

## 6. Önerilen Aksiyon Sırası

1. **H1** — `quartz.rs` scroll eksenlerini düzelt + saf test + simüle backend assertion. (Gerçek macOS'ta ORIENT "no change" döngüsüne sebep olan birincil aday.)
2. **H2** — `agent.py` checkpoint guard'ını düzelt + test.
3. **M1** — `last_error` temizleme semantiğini yorumla uyumlu hale getir + test.
4. **M2** — `decide_permission`'a `ROUTINE` dalı ekle (veya ölü kodu kaldır) + test.
5. **M8** — CI'ya `macos-latest` Rust job'ı ekle (bu, H1 gibi hataları kalıcı olarak yakalar).
6. **M3, M4** — state taşıma tutarlılığı ve decode-performansı.
7. **M5, M6, L-serisi** — süreç/dokümantasyon iyileştirmeleri.

---

## 7. Uygulama Kaydı (Fix Log) — 2026-09-01

Tüm bulgular faz faz uygulandı (AGENTS.md §10: understand → research → plan → implement → test → review → fix → regression-test → validate). Araştırma adımında context7 MCP + `skills.sh` (find-skills + apollographql rust-best-practices) kullanıldı.

| Bulgu | Durum | Değişiklik | Regresyon testi |
|---|---|---|---|
| H1 scroll eksenleri | ✅ | `driver/src/quartz.rs`: `scroll_wheels(dx,dy)` saf fonksiyonu — dy→wheel1 (dikey), dx→wheel2 (yatay); test modülü dosya sonuna taşındı (clippy `items-after-test-module`) | `scroll_wheels_*` (3 birim testi) |
| H2 checkpoint=0 | ✅ | `agent.py`: `"runner" in locals()` guard'ı kaldırıldı; callback `runner.executed_trajectory` uzunluğunu yazar | `test_checkpoint_records_real_step_count` |
| M1 last_error temizleme | ✅ | `loop.py`: başarılı eylem `last_error`'u temizler (tekrar ipucu hariç); runner aynası `finish` dışında temizlenir | `test_success_after_failure_clears_last_error` |
| M2 ROUTINE ölü politika | ✅ | `autonomy.py`: GUARDED + ROUTINE → CONFIRM (marker listesi canlı politika oldu); docstring güncellendi | `test_level2_guarded_confirms_routine_stateful_actions`, `test_level3_full_auto_runs_routine_actions` |
| M3 open_tabs düşüşü | ✅ | `loop.py`: 7 WorkingState yeniden kurulumuna `open_tabs=state.open_tabs` eklendi (skill-mount dahil) | `test_retrieve_preserves_open_tabs_after_mount` |
| M4 tam-kare decode | ✅ | `capture.py`: `crop_capture` (ham BGRA baytlarını önce kırpar); ORIENT artık yalnızca bölgeyi decode eder | `test_crop_capture_*` (2 test) |
| M5 PID yeniden kullanımı | ✅ | `menu.rs`: `process_is_menu_launcher` (libc::proc_name) — SIGTERM yalnızca aynı launcher sürecine; paketli ad (`ComputerUse`) + kendi adıyla karşılaştırma (kurulum sırasında doğrulandı: dev binary paketli app'i tanıyıp tek-örnek kuralını uyguladı) | — (macOS gated) |
| T1 TCC izni her kurulumda düşüyor | ✅ | Ad-hoc imza her rebuild'de cdhash kimliği değiştirip Accessibility iznini geçersiz kılıyordu. Çözüm: kalıcı self-signed sertifika (`ComputerUse Dev`, login keychain, 10 yıl, codeSigning EKU + trustRoot) — `scripts/make_signing_cert.sh`; `package_app.sh` artık bu kimlikle ve sabit `--identifier`'larla imzalıyor (app: `com.computeruse.app`, sürücü: `com.computeruse.driver`, bundle `--deep`'siz). Designated requirement: `identifier "com.computeruse.app" and certificate leaf = H"484f…"` — rebuild'lerde sabit; izin bir kez verilir, kurulumlarda korunur | kurulumda doğrulandı (`codesign --verify --deep --strict` OK) |
| T2 Geçici TLS/ağ hataları çalışmayı öldürüyor | ✅ | `SSLV3_ALERT_BAD_RECORD_MAC` gibi geçici wire hataları retry'siz terminal `OpenAIError`'e dönüşüyordu (AGENTS.md §7.2 ihlali). Çözüm: `openai.py` — timeout/ConnectionError/SSLError için exponential backoff (3 deneme, 0.5s/1s/2s, warning log); HTTP hataları terminal kalır (kullanıcıya ne düzelteceğini söyler: key/quota/model) | `test_openai_model_retries_transient_tls_failures` |
| M6 launcher onay kanalı | ✅ | `menu.rs` piped stdin + `cmd:confirm` bridge; `cli.py` `COMPUTERUSE_MENU` altında pipe-safe `readline` handler; `menu.html` Approve/Deny kartı | — |
| M7 fiyat yorumları | ✅ | `openai.py` docstring: 2026-07-30 indirimi (Terra $2/$12, Luna $0.20/$1.20, Sol $5/$30; ~2.5x) | — |
| M8 CI macOS kapsamı | ✅ | `.github/workflows/ci.yml`: `macos-latest` Rust job (build + test + clippy — macOS-gated modüller artık CI'da derlenir) | — (CI) |
| L1 aksiyon sayısı | ✅ | AGENTS.md §4 + README: 11 aksiyon (`clipboard_paste`, `activate_app` eklendi) | — |
| L2 ADR-3 transport | ✅ | AGENTS.md: `openai` SDK → stdlib `urllib` | — |
| L3 ağaç şeması | ✅ | AGENTS.md/README: `planner.py`, `som.py`, `memory/schemas.py`, `focus.py`, driver modülleri eklendi; `tests/unit/` kaldırıldı | — |
| L4 sea-blue → emerald | ✅ | AGENTS.md + README (uygulama `#50A574` emerald) | — |
| L5 prompt/schema varsayılanları | ✅ | `prompts.py`: drag 400→200, wpm 50→40 (şemalarla eşitlendi) | — |
| L6 bayat docstring | ✅ | `killswitch.py`: "None-condition ahead" metni düzeltildi | — |
| L8 som.py iddiaları | ✅ | `som.py`: "numbered badges" yanlış iddiası düzeltildi (düz kareler); `[:30]` sessiz tavanı kaldırıldı; bağlanmamış durum belirtildi | — |
| L9 gereksiz RPC | ✅ | `agent.py`: tek focused-window okuması tüm probe'larla paylaşılıyor (pid önbelleği) | mevcut probe testleri |
| L10 TypeError maskeleme | ✅ | `prompts.py`: `_supports_image_argument` imza kontrolü; iç hata artık yayılır | `test_call_model_*` (2 test) |
| L11 attribute injection | ✅ | `menu.html`: inline onclick kaldırıldı — her iki buton `addEventListener` + `textContent` | — |
| L12 bayat "Not yet implemented" | ✅ | README: vision input canlı; SoM wiring yeni frontier olarak işaretlendi | — |
| L13 test düzeni | ✅ | `tests/test_prompts.py` silindi (kapsam `tests/smoke/test_prompts.py`'ye taşındı: `click` + `Tab` alias'ları eklendi) | konsolide testler |
| L14 pacing imza kırılganlığı | ✅ | `distiller.py`: `duration_ms`, `wpm` `_SEMANTIC_KEYS`'ten çıkarıldı | `test_distill_signature_ignores_pacing_fields` |
| L15 sabit "64" metni | ✅ | `agent.py`: f-string ile `AX_MAX_ELEMENTS`'ten türetiliyor | — |

**Doğrulama (hepsi yerel macOS):** `pyright` 0 hata/0 uyarı · `ruff` temiz · `pytest` **247 passed** (256 − 9 konsolide legacy test + yeni regresyonlar) · `cargo test` **27 passed** (24 + 3 scroll) · `cargo clippy -D warnings` temiz.

Kapsam dışı (bilinçli): L7 (`_verify_finish` yorumu — RPC azaltma zaten L9 ile yapıldı; yorum hâlâ geçerli çünkü probe'lar ORIENT'ten ayrı), L16 (`detail:high` bilinçli kalite takası), SoM'un OODA'ya bağlanması (yeni özellik).

---

## 7b. Özellik Kaydı (Yeni Özellikler) — 2026-09-01

### F1 — Canlı kullanım sayaçları (token + süre) ve hız ayarı
- **Neden:** Kullanıcı raporu: çalışma yavaş ve arayüzde token/toplam süre görünmüyor.
- `providers/openai.py`: her model çağrısı `ModelCallStats` (prompt/completion/total token + süre) döner; `usage` ayrıştırması string-tolerant `usage_int()` ile strict-typed (pyright 0 hata).
- `cli.py`: `stats_sink` her başarılı çağrıdan sonra `st : tok_total=… elapsed=…s calls=…` satırı basar; `_accepts_keyword()` imza kontrolü sayesinde legacy transport'lara `stats_sink` zorla geçirilmez.
- `menu.html`: header'da `⚡ N tok` ve `⏱ Ns` canlı pill'ler; History kartlarında token rozeti.
- **Hız:** model görüntüsü `detail: low`'a çekildi (her turda ~1100+ imge token tasarrufu; görüntüler AX ile desteklenen doğrulama amaçlıdır, detay kaybı kabul edilebilir — L16 kararına paralel).

### F2 — Canlı akış referans tasarımı (kullanıcının paylaştığı feed görseliyle birebir dil)
- Grup deseni: `💻 Bilgisayara bağlandı` (run başlangıcı), `◎` sub-goal başlıkları grup başlığı ve alt satırları (observe/action/verify) girintili çocuklar, `💡` muhakeme satırları grubu kapatıp üst düzeyde sürer, bitişte `🔍 Toplam N adım · M token · N çağrı` grubu + girintili çocuklar (`⚡ token kullanıldı`, `⏱ saniye sürdü`, `🖱 fiziksel eylem`).
- Bayrak satırı temizliği döngülü regex ile sağlamlaştırıldı (repeat-marker kombinasyonlarına dayanıklı).
- `cli.py` `st :` satırına `calls=N` eklendi (grup başlığında model çağrı sayısı).

**Kanıt:** pytest **248 passed** · pyright 0/0/0 · ruff temiz · JS `node --check` OK · preview simülasyonu DOM doğrulaması (connect satırı, 2 grup + girintili çocuklar, 🔍 toplam grubu, temiz bayrak satırı, canlı pill'ler) · `codesign --verify --deep --strict` OK (kalıcı sertifika sayesinde Erişilebilirlik izni korundu).

---

## 8. Kaynaklar / Kanıt Listesi

- Yerel kapı çıktıları: `pyright` (0/0/0), `ruff` (temiz), `pytest` (247 passed), `cargo test` (27 passed), `cargo clippy -D warnings` (temiz).
- core-graphics 0.25.0 kaynağı (`~/.cargo/registry/src/.../core-graphics-0.25.0/src/event.rs:730-750`): `new_scroll_event` imzası.
- OpenAI GPT-5.6 fiyat duyurusu: openai.com (`/index/gpt-5-6/`, `/index/previewing-gpt-5-6-sol/`), openrouter.ai, cloudzero.com — 2026-07-30 indirimi (Terra $2/$12, Luna $0.20/$1.20).
- `uv.lock`: pydantic 2.13.5; `driver/Cargo.lock`: core-graphics 0.25.0, objc2 0.6.4, objc2-app-kit 0.3.2, objc2-web-kit 0.3.2.
- Python 3.14.3 / uv 0.10.9 / cargo 1.94.0 (bu makine).
