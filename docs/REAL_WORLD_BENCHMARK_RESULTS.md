# 🌐 GERÇEK DÜNYA ÜRETİM WEB SİTELERİ DENETİM VE TEST SONUÇLARI

Bu belge, otonom bilgisayar kullanım ajanının (Computer Use Agent) yerel veya kontrollü test sayfaları yerine **doğrudan internet üzerindeki üçüncü parti gerçek sitelerde** gerçekleştirdiği uçtan uca işlemlerin doğrulama kanıtlarını içerir.

---

## 📊 İcra ve Başarı Karnesi

| Görev / Hedef Site | İşlem Kapsamı | İcra Süresi | AX Düğüm Sayısı | Durum |
|---|---|---|---|---|
| **1. [Wikipedia (TR)](https://tr.wikipedia.org)** | "Yapay zekâ" maddesinde arama, iç bağlantı tespiti (`Tarihçe`) ve derin sayfa geçişi | 8.76 sn | 4161 | ✅ **%100 BAŞARILI** |
| **2. [GitHub](https://github.com/astral-sh/uv)** | Canlı repo analizi, AX koordinat çözümlemesi ve "Releases" sekmesine geçiş | 9.79 sn | 3505 | ✅ **%100 BAŞARILI** |
| **3. [Hacker News](https://news.ycombinator.com)** | Canlı teknoloji gündemi ayrıştırma, 1. sıradaki haberin tespit edilmesi ve yorum tartışmasına girilmesi | 8.68 sn | 2947 | ✅ **%100 BAŞARILI** |

* **Toplam Görev Süresi:** `29.24 saniye`
* **Genel Başarı Oranı:** `%100` (3/3 Tamamlandı)
* **Kullanılan Sürücü:** macOS Quartz Micro-Driver (`actuation-driver --real`)

---

## 📸 Canlı Doğrulama ve Ekran Kanıtları

1. **Wikipedia Derin Gezinti:** [`target/real-world-external-benchmarks-20260906/wikipedia_witness.png`](file:///Users/dogan/Desktop/computeruse/target/real-world-external-benchmarks-20260906/wikipedia_witness.png)
2. **GitHub Canlı Sürüm İncelemesi:** [`target/real-world-external-benchmarks-20260906/github_witness.png`](file:///Users/dogan/Desktop/computeruse/target/real-world-external-benchmarks-20260906/github_witness.png)
3. **Hacker News Tartışma Odaklanması:** [`target/real-world-external-benchmarks-20260906/hackernews_witness.png`](file:///Users/dogan/Desktop/computeruse/target/real-world-external-benchmarks-20260906/hackernews_witness.png)

---

## 📑 Detaylı Görev Raporları

* Wikipedia Raporu: [`target/real-world-external-benchmarks-20260906/WIKIPEDIA_ARASTIRMA_RAPORU.md`](file:///Users/dogan/Desktop/computeruse/target/real-world-external-benchmarks-20260906/WIKIPEDIA_ARASTIRMA_RAPORU.md)
* GitHub Analiz Raporu: [`target/real-world-external-benchmarks-20260906/GITHUB_ANALIZ_RAPORU.md`](file:///Users/dogan/Desktop/computeruse/target/real-world-external-benchmarks-20260906/GITHUB_ANALIZ_RAPORU.md)
* Hacker News Raporu: [`target/real-world-external-benchmarks-20260906/HACKERNEWS_CANLI_ANALIZ.md`](file:///Users/dogan/Desktop/computeruse/target/real-world-external-benchmarks-20260906/HACKERNEWS_CANLI_ANALIZ.md)
* Master JSON: [`target/real-world-external-benchmarks-20260906/MASTER_REAL_WORLD_SUMMARY.json`](file:///Users/dogan/Desktop/computeruse/target/real-world-external-benchmarks-20260906/MASTER_REAL_WORLD_SUMMARY.json)

---

## 🧪 2026-09-08 — Yeni Güvenlik/Bellek Katmanlarının Gerçek-Host Doğrulaması

Sovereign mode, ranked scheduler (CLI migrasyonu sonrası) ve adaptive preference memory'nin simulated backend dışındaki ilk ölçümü. Scratch store (`/tmp/cu-realcheck`) kullanıldı, gerçek hafıza kirletilmedi. Sürücü kaynaktan taze derlendi (`cargo build --release`, 11.31 sn), model `gpt-5.6-terra`, ön uygulama `T3 Code (Nightly)`.

| # | Hedef | Adım | Token | Gerçek Maliyet | Sonuç |
|---|---|---|---|---|---|
| 1. Sovereign smoke (`--sovereign`, sıfır-eylem hedefi) | Ön pencere başlığını raporla, ekrana dokunma | 1 | 12.064 | $0.025 | ✅ success, completion-auditor onaylı, ekranda değişiklik yok |
| 2. Preference, sıfır-eylemli goal (`tercih: cevap dili = Türkçe` + salt-okuma) | Başlığı raporla | 1 | 15.698 | $0.033 | ✅ success ama tercih **yazılmadı** — tasarım gereği: trajectory boşken `on_complete` öğrenmeden erken döner |
| 3. Scheduler, inbox yolu (`--autonomous 1 --watch`) | `task1.txt`'teki hedefi çalıştır | 1 | 15.807 | $0.033 (makbuza **$0.00** yazıldı — Bug 2) | ✅ success; `task file 'task1.txt' claimed…` logu, `.processed/` arşivi doğrulandı |
| 4. Preference, tek-tıklamalı goal (Aşama 2 tekrarı) | Pencereye 1 odak tıklaması + başlığı raporla | 2 | 26.381 | $0.059 | ✅ success; `general.cevap-dili = Türkçe` (conf 1.0, explicit, evidence=run_id) store'a yazıldı ve `active()` ile geri okundu |

* **Toplam:** `4/4 success`, `69.950 token`, gerçek harcama `~$0.15`
* **Faz metrikleri (`phase_s`)** dört run'da da mevcut: karar turu (decide_s ≈ 2.5–6.4 sn) baskın maliyet; gözlem + eylem ≈ 4–8 sn.

### Deneyin bulduğu ve aynı gün kapatılan iki bug

1. **Rapor harcamayı gizliyordu:** sıfır-episode'lu run'ların makbuzları `--report`'ta görünmüyor, spend satırı hiç yazılmıyordu (`is_quiet` usage'a bakmıyordu). Düzeltme: `run_id` join anahtarıyla `usage_only` render edilir.
2. **Otonom oturumlar $0.00 faturalıyordu:** session `stats_sink`'i stats objesindeki var-olmayan `cost_usd` alanını okuyordu; sonuç olarak tüm otonom makbuzlar $0.00 ve `--max-cost` tavanı otonomda **asla tetiklenmiyordu**. Düzeltme: single-run ile birebir aynı kural (startup'ta fiyat çözümleme + `call_cost_usd`).
