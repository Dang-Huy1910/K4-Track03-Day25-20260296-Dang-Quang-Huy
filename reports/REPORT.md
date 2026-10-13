# Lab 25B — Report

**Họ tên / nhóm:** Đặng Quang Huy / 20260296  **Commit:** c15049092337825141b8c1c8c4dc886d0c771577  **Điểm tự động (`make grade`):** 84 / 88

> Phần E ẩn (+4) do giảng viên chấm. Mọi số dưới đây lấy từ `reports/gameday/*.json`, `reports/workflows.json`, `reports/slo.json`, `reports/cache/*.json` (seed 25).

## 1. Kết quả Game Day (naive vs của bạn)

| Băng | Client | availability | p95 (s) | amplification | retry_ratio | cost/turn | recovery (s) | PASS? |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
| G1 | naive | 0.7194 | 7.157 | 7.738 | 6.738 | 2.163 | 4.0 | FAIL |
| G1 | yours | 1.0000 | 4.776 | 1.062 | 0.002 | 2.101 | 0.0 | PASS |
| G2 | naive | 0.4560 | 15.034 | 9.904 | 8.904 | 4.627 | 155.0 | FAIL |
| G2 | yours | 1.0000 | 10.920 | 1.041 | 0.007 | 2.119 | 0.0 | PASS |
| G3 | naive | 1.0000 | 4.768 | 1.164 | 0.164 | 2.328 | 0.0 | FAIL |
| G3 | yours | 1.0000 | 4.660 | 1.060 | 0.034 | 2.145 | 0.0 | PASS |
| G4 | naive | 1.0000 | 13.764 | 1.004 | 0.004 | 2.008 | 0.0 | FAIL |
| G4 | yours | 1.0000 | 5.562 | 1.015 | 0.002 | 2.072 | 0.0 | PASS |
| G5 | naive | 0.3699 | 5.359 | 17.030 | 16.030 | 2.124 | — | FAIL |
| G5 | yours | 0.8179 | 5.477 | 1.003 | 0.002 | 2.504 | — | PASS |
| G6 | naive | 0.5170 | 4.836 | 12.344 | 11.344 | 2.009 | 0.0 | FAIL |
| G6 | yours | 1.0000 | 5.961 | 1.055 | 0.019 | 2.189 | 0.0 | PASS |

## 2. Cấu hình client và lý do

| Tham số | Giá trị | Vì sao (dựa trên băng nào, số liệu nào) |
|---|---:|---|
| attempt_timeout_s | 8.0 | G6: alpha-eu treo; timeout 8 s cho phép fail over còn ~20 s trong deadline 30 s. Thí nghiệm timeout=30 làm avail G6 tụt còn 0.95 (deadline give-up). |
| max_attempts_per_provider | 2 | 1 retry cùng provider rồi spillover. G3: retry_ratio=0.034 ≤ 0.10; G1 amplification=1.062 ≤ 1.6. |
| backoff_base_s / cap_s | 0.25 / 4.0 | Full jitter; đủ nhỏ để không đốt deadline, đủ lớn để tránh đồng pha khi 529. |
| retry_budget_ratio | 0.1 | Giữ retry ≤ ~10% first attempts. G3 retry_ratio=0.034; khi breaker bị vô hiệu hóa, budget vẫn kìm amplification G2 ở 1.38 thay vì 1.68. |
| breaker.window_size / min_calls | 20 / 10 | Tránh trip sớm trên blip; đủ nhanh để bắt brown-out/overload. |
| breaker.failure_rate_threshold | 0.5 | G2/G3: 50% lỗi trong cửa sổ → OPEN, spillover sang alpha-eu/beta. |
| breaker.slow_call_s | 5.0 | G4: alpha chậm +5 s; slow success trip breaker → p95 5.56 (naive 13.76). |
| breaker.open_duration_s | 10.0 | Cho provider thở; G2 recovery_s=0 (naive 155 s). |
| breaker.half_open_max_probes | 2 | Tránh thundering herd khi HALF_OPEN. |

Sơ đồ quyết định của `on_error`:

```
classify(err)
  CLIENT_ERROR     -> release permit; GiveUp (không retry, không failover)
  QUOTA_EXHAUSTED  -> release; cooldown quota_cooldown_s; failover ngay
  RATE_LIMITED     -> release; cooldown until now+retry_after; failover ngay
  OVERLOADED /
  SERVER_ERROR /
  TIMEOUT          -> on_failure(breaker);
                      nếu tries < max AND budget.acquire AND còn thời gian
                        -> retry CÙNG provider sau backoff.delay(tries)
                      else -> failover (idx++, tries=0, delay=0)
ladder exhausted   -> chờ cooldown rate-limit ngắn (<=5s) nếu còn deadline
                      else GiveUp("no_provider_available")
```

## 3. Ba thí nghiệm (mỗi cái 3–5 câu + số)

1. **Retry budget là lớp phòng thủ cuối.** Breaker bị vô hiệu (`min_calls: 1e9`) và `max_attempts_per_provider: 5`. G2 với budget bật: ampl=1.38, retry=0.058, avail=0.999, recovery=0. G2 tắt budget: ampl=1.678, retry=0.494, avail=0.966, recovery=20 s. G3: budget bật retry=0.068; tắt budget retry=0.159 (vượt ngưỡng 0.10 của băng). Khi breaker đúng, budget ít “cứu” thêm vì failover đã cắt retry; vẫn nên giữ cả hai vì budget là backstop khi breaker cấu hình sai hoặc lỗi không đếm vào breaker (rate limit).

2. **`attempt_timeout_s: 30`.** G6 baseline (timeout 8): avail=1.0, p95=5.96, give_up={}. Timeout 30: avail=0.949, p95=5.32, give_up={deadline:109, no_provider_available:13}. p95 *giảm* dù availability tụt — survivorship bias: các lượt treo 30 s rồi hết hạn không vào mẫu p95. G4 gần như không đổi (p95 5.56 → 5.61) vì breaker slow-call đã cắt alpha trước khi timeout phát huy. Timeout đúng nên dựa trên p99 lúc bình thường (~2–3 s) cộng margin nhỏ, và còn đủ thời gian cho ít nhất một lần failover trong deadline user 30 s → ~8 s là hợp lý.

3. **Bỏ lọc tool calling khỏi `ladder()`.** G6: incompatible_routes=234, avail=0.902 (baseline 1.0, 0 incompatible). `gamma-mini` nhận 454 attempts tool call nhưng không hỗ trợ tool → model trả output không dùng được. Với product manager: khoảng 10% lượt user trong outage nhận câu trả lời lỗi kỹ thuật thay vì được chuyển sang beta; đó là regression UX có thể đo bằng `incompatible_routes` trong CI.

## 4. Workflow hoàn tiền

| Executor | completion | crashes | duplicate refunds | duplicate emails | lost refunds | overpaid | wasted LLM / crash |
|---|---:|---:|---:|---:|---:|---:|---:|
| naive | 1.0 | 81 | 128 | 81 | 0 | 1_496_700¢ | 1.123 |
| yours | 1.0 | 66 | 0 | 0 | 0 | 0 | 0.303 |

Chỉ idempotency key thì khi worker crash *sau* khi payment đã chạy nhưng *trước* khi lưu kết quả, worker mới vẫn gọi lại step (an toàn nhờ key) nhưng phải chạy lại mọi bước LLM phía trước → `wasted_llm_per_crash` cao. Chỉ checkpoint mà không có key ổn định: nếu timeout mất response rồi retry với key mới → hoàn tiền trùng. Ghi checkpoint *trước* khi gọi payment: crash giữa checkpoint và payment thật → resume bỏ qua step → `lost_refunds`. Đúng thứ tự: execute → rồi mới `store.put`.

## 5. Alerting & error budget

| Sự cố | Mong đợi | Naive alerter | Burn-rate alerter | Độ trễ phát hiện |
|---|---|---|---|---:|
| checkout_bug | page | page (1 phút) | page | 27 phút |
| provider_outage | page | page (0) | page | 4 phút |
| deploy_blip | none | **false page** | none | — |
| slow_leak | ticket | miss | ticket | 1337 phút |
| degraded_evening | page | page | page | 291 phút |
| night_spike | none | **false page** | none | — |
| small_bump | none | **false page** | none | — |

Error budget 30 ngày: đã tiêu **42.95%**, còn **57.05%**. Không đóng băng release (`freeze_releases=False`). `small_bump` (20 phút ở 12% lỗi) không nên page lúc 11 giờ trưa vì cửa sổ dài 1 h của luật 14.4× chưa đạt ngưỡng burn — đó là nhiễu ngắn, không đốt đáng kể budget tháng; page sẽ tạo alert fatigue.

## 6. Postmortem — Băng G2 với client naive (blameless)

*Hình: `reports/gameday/G2_overload_storm.png`. CSV: `reports/gameday/naive_G2_overload_storm_timeseries.csv`.*

**Tóm tắt (3 câu):** Alpha mất 75% GPU trong 3 phút (t=120–300). NaiveClient nhân mỗi logical call lên tới 36 attempt với delay cố định, không tôn trọng retry-after, không breaker/fallback. Tải retry đẩy provider vào trạng thái metastable: sau khi GPU hồi phục ở t=300, hệ thống vẫn gần như 0% success đến ~t=455 (recovery ≈ 155–185 s).

**Ảnh hưởng:** 2362 lượt user, chỉ 1077 thành công (avail 45.6%); 1285 lượt thất bại trong ~8 phút mô phỏng. Amplification 9.9×, cost/turn 4.63 (gấp đôi resilient). Giả sử tháng chỉ có sự cố này: ~1285 bad trên ~(30×24×60×4×2 calls) — đốt một phần lớn error budget 0.5% của SLO 99.5%.

**Timeline** (giây ảo — lấy từ CSV, bucket 10 s):

| t | Sự kiện | Bằng chứng (số) |
|---:|---|---|
| 0–110 | Bình thường | ~7–9 attempts/s, success ratio ≈ 1.0 |
| 120 | alpha mất 75% năng lực | capacity_factor giảm; attempts bắt đầu tăng |
| 130–140 | Retry storm khởi động | att/s 52 → 214; sr vẫn cao rồi đổ về 0 |
| 150–290 | Sập hoàn toàn trong sự cố | sr=0.0; att/s duy trì 110–170 (xa trên năng lực còn lại) |
| 300 | alpha hồi phục hoàn toàn | capacity trở lại; att/s còn 39 nhưng sr vẫn 0 |
| 300–440 | Metastable: chưa tự hồi phục | hàng đợi đầy request client đã bỏ; retry tiếp tục giữ tải > capacity (att/s 15–130, sr=0) |
| 453 | Success đầu tiên sau restore | calls_ok>0 lần đầu sau t=300 |
| ~485 | Hồi phục 95% trong cửa sổ 30 s | recovery_s metric ≈ 155–185 s |

**Trigger vs root cause:** Trigger = mất 75% GPU trên alpha trong 3 phút. Root cause = ba tầng retry chồng nhau (4×3×3), delay cố định, không jitter, không retry budget, không circuit breaker, không failover → mỗi lỗi tạo thêm tải → hàng đợi dài → timeout → thêm retry (vòng lặp metastable). Provider đã khỏe lại nhưng vẫn nghẹt vì chính lượng retry của client.

**Điều gì giúp / điều gì làm tệ hơn:** Tôn trọng `retry-after` và failover (resilient: 0 violation, avail 1.0, recovery 0) giúp ngay. Retry cố định 0.2 s trên cùng provider làm tệ hơn (28381 retry-after violations trên G2 naive).

**Action items** (cụ thể, có owner, đo được):

| # | Hành động | Loại (prevent / detect / mitigate) | Owner | Cách kiểm chứng |
|---|---|---|---|---|
| 1 | Một tầng retry + full jitter + retry budget 10% trong ResilientClient | prevent | Platform / agent runtime | `amplification ≤ 1.2` trên G2 trong CI (`make gameday`) |
| 2 | Circuit breaker sliding-window + slow-call trên mọi deployment | mitigate | Platform | G2 `recovery_s ≤ 30`; G4 `latency_p95_s ≤ 10` |
| 3 | Burn-rate alerting (14.4×/6×) + dashboard amplification & retry_after_violations | detect | SRE | 0 false page trên blip; page G2-like trong ≤ 5 phút trên dữ liệu SLI |

## 7. Một điểm yếu còn lại

G2 resilient vẫn có p95 ≈ 10.9 s (sát ngưỡng trải nghiệm dù avail=100%). Khi alpha chậm/overloaded, một phần traffic spill sang beta (cost 1.4×) và probe HALF_OPEN vẫn trả chậm trước khi trip lại. Hướng sửa: giảm `attempt_timeout_s` xuống ~5–6 s khi ladder còn provider dự phòng, hoặc hedged request ngắn sang alpha-eu song song với primary khi latency ước lượng vượt p95 mục tiêu.

## 8. Cache (Part G)

Số từ `reports/cache/*.json` (`make cache`).

| Client | availability | faq_availability | cache_hit_ratio | stale_served | wrong_answers | outdated_answers | cost/turn | PASS? |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| resilient (không cache) | 0.8373 | 0.8424 | 0.000 | 0 | 0 | 0 | 1.386 | FAIL |
| cached (của bạn) | 0.9117 | 0.9640 | 0.681 | 140 | 0 | 13 | 0.896 | PASS |

| Tham số `cache:` | Giá trị | Vì sao (số liệu nào) |
|---|---:|---|
| similarity_threshold | 0.92 | thr=0.80 → wrong=165 (`other_question`); 0.92 → wrong=0, hit=0.681; 0.97 → wrong=0 nhưng hit tụt còn 0.623, cost tăng. |
| ttl_s | 90 | ttl=300 → outdated=95; ttl=90 → 13 (≤15); ttl=30 → outdated=2 nhưng hit chỉ 0.591. 90 cân bằng freshness và hit. |
| max_stale_s | 180 | Phủ outage 120 s (t=240–360). max_stale=0 → faq_avail=0.883 (FAIL ≥0.93), stale_served=0. |

Thí nghiệm:

1. **Ngưỡng.** thr 0.80 / 0.92 / 0.97: wrong 165 / 0 / 0; hit 0.685 / 0.681 / 0.623; cost 0.955 / 0.896 / 0.935. Ở 0.80 các cặp FAQ “gần” (chính sách hoàn tiền ↔ thời gian hoàn / phí ship) bị nhầm (`other_question`).
2. **TTL.** Webhook chỉ tới khi CMS báo (`policy/refund` lúc t=150). Đổi bảng phí ship lúc t=480 **không** có webhook → invalidation không chạy; chỉ TTL (và hết stale) giới hạn `outdated_answers`. ttl=300 giữ câu trả lời phí ship cũ quá lâu (outdated=95).
3. **Stale-if-error.** `max_stale_s: 0`: faq_availability 0.883 (baseline 0.964), stale_served=0 — trong 2 phút outage FAQ miss hoàn toàn vì không được phục vụ bản stale.

Trả lời:

- Câu hỏi cá nhân không cache (kể cả per-user) vì rủi ro lộ đơn/điểm của user khác nếu scope/user_id sai, và câu trả lời gắn dữ liệu thay đổi nhanh hơn FAQ; `request.personal` bị loại khỏi allowlist → `other_user` = 0.
- Khi provider sập mà dashboard xanh nhờ cache: alert theo `stale_served` tăng + `cache_hit_ratio` bất thường cao kết hợp provider error rate / breaker OPEN — đừng chỉ nhìn availability tổng, vì stale-if-error che mất outage.
