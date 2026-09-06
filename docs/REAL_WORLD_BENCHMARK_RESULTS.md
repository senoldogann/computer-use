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
