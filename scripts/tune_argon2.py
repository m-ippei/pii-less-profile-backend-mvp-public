"""Argon2id のパラメータ調整用ベンチマーク（VPS 上で実行する）。

使い方:
    deploy/deploy.sh tune                   # 候補を計測し、推奨の ARGON2_* を表示
    deploy/deploy.sh tune --min-ms 250 --max-ms 450

何をするか:
    メモリコスト・時間コストの候補ごとに、1 回の hash 時間（中央値）と、
    ARGON2_MAX_CONCURRENCY 並列で実行したときの時間を測る。
    目標範囲（既定 200〜500ms）に収まる候補のうち、メモリが最大のものを推奨する。

注意:
    - 計測中は他の負荷を避ける（staging では app を止めなくてもよいが、アクセスが無い時間帯に）。
    - 変更が効くのは「新しく作る hash」だけ。既存の hash は自身のパラメータで検証される。
      利用者が増える前（公開前）に決めるのが望ましい。
    - 本番相当（ENV!=test）は time_cost>=2 かつ memory_kib>=19456 を下回る設定だと起動しない。
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass

from argon2 import PasswordHasher, Type

MIN_TIME_COST = 2
MIN_MEMORY_KIB = 19456
MEMORY_CANDIDATES_KIB = (19456, 32768, 65536, 131072, 262144)
MAX_TIME_COST = 6
SAMPLE_PASSWORD = "benchmark-only-not-a-real-password"


@dataclass(frozen=True)
class Result:
    memory_kib: int
    time_cost: int
    single_ms: float
    concurrent_ms: float


def make_hasher(time_cost: int, memory_kib: int, parallelism: int) -> PasswordHasher:
    return PasswordHasher(
        time_cost=time_cost,
        memory_cost=memory_kib,
        parallelism=parallelism,
        type=Type.ID,
    )


def measure_single(hasher: PasswordHasher, samples: int) -> float:
    hasher.hash(SAMPLE_PASSWORD)  # ウォームアップ（計測に含めない）
    times: list[float] = []
    for _ in range(samples):
        start = time.perf_counter()
        hasher.hash(SAMPLE_PASSWORD)
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


def measure_concurrent(hasher: PasswordHasher, workers: int, samples: int) -> float:
    times: list[float] = []
    lock = threading.Lock()

    def work() -> None:
        for _ in range(samples):
            start = time.perf_counter()
            hasher.hash(SAMPLE_PASSWORD)
            elapsed = (time.perf_counter() - start) * 1000
            with lock:
                times.append(elapsed)

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return statistics.median(times)


def available_memory_kib() -> int | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def peak_memory_kib() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Argon2id parameter benchmark")
    parser.add_argument("--min-ms", type=float, default=200.0, help="目標の下限(ms)")
    parser.add_argument("--max-ms", type=float, default=500.0, help="目標の上限(ms)")
    parser.add_argument("--parallelism", type=int, default=4, help="ARGON2_PARALLELISM")
    parser.add_argument("--max-concurrency", type=int, default=2, help="ARGON2_MAX_CONCURRENCY")
    parser.add_argument("--samples", type=int, default=5, help="候補ごとの計測回数")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.min_ms >= args.max_ms:
        print("--min-ms は --max-ms より小さくしてください", file=sys.stderr)
        return 2

    cpus = os.cpu_count() or 1
    avail = available_memory_kib()
    print(f"vCPU={cpus}  MemAvailable={'?' if avail is None else f'{avail // 1024}MiB'}")
    print(
        f"目標: 1回あたり {args.min_ms:.0f}〜{args.max_ms:.0f}ms"
        f" / 同時実行 {args.max_concurrency} / parallelism {args.parallelism}"
    )
    if cpus < args.parallelism:
        print(
            f"注意: vCPU({cpus}) < parallelism({args.parallelism})。"
            "並列度は時間を短縮しないため、メモリ・時間コストで調整する。"
        )
    print()
    print(f"{'memory_kib':>11} {'time':>5} {'single_ms':>10} {'concurrent_ms':>14}  判定")

    results: list[Result] = []
    for memory_kib in MEMORY_CANDIDATES_KIB:
        if avail is not None and memory_kib * args.max_concurrency > avail // 2:
            print(f"{memory_kib:>11}  -- 空きメモリの半分を超えるためスキップ")
            continue
        for time_cost in range(MIN_TIME_COST, MAX_TIME_COST + 1):
            hasher = make_hasher(time_cost, memory_kib, args.parallelism)
            single = measure_single(hasher, args.samples)
            if single > args.max_ms * 1.5:
                print(f"{memory_kib:>11} {time_cost:>5} {single:>10.0f} {'-':>14}  遅すぎる")
                break
            concurrent = measure_concurrent(hasher, args.max_concurrency, args.samples)
            ok = args.min_ms <= single <= args.max_ms
            mark = "◎ 目標内" if ok else ("速すぎ" if single < args.min_ms else "やや遅い")
            print(f"{memory_kib:>11} {time_cost:>5} {single:>10.0f} {concurrent:>14.0f}  {mark}")
            results.append(Result(memory_kib, time_cost, single, concurrent))
            if single > args.max_ms:
                break

    in_range = [r for r in results if args.min_ms <= r.single_ms <= args.max_ms]
    print()
    if not in_range:
        mid = (args.min_ms + args.max_ms) / 2
        if not results:
            print("計測できる候補がありませんでした（メモリ不足の可能性）。")
            return 1
        best = min(results, key=lambda r: abs(r.single_ms - mid))
        print("目標範囲に収まる候補がありません。最も近い候補を表示します。")
    else:
        # OWASP の考え方に倣い、メモリを優先し、同じメモリなら time_cost が大きいものを選ぶ。
        best = max(in_range, key=lambda r: (r.memory_kib, r.time_cost))

    print("推奨（.env に設定）:")
    print(f"ARGON2_TIME_COST={best.time_cost}")
    print(f"ARGON2_MEMORY_KIB={best.memory_kib}")
    print(f"ARGON2_PARALLELISM={args.parallelism}")
    print(f"ARGON2_MAX_CONCURRENCY={args.max_concurrency}")
    print(
        f"\n(単独 {best.single_ms:.0f}ms / {args.max_concurrency}並列 {best.concurrent_ms:.0f}ms"
        f" / 同時実行時の Argon2 メモリ ≒ {best.memory_kib * args.max_concurrency // 1024}MiB)"
    )
    if best.concurrent_ms > 1000:
        print(
            "警告: 並列時が1秒を超えますvCPUが不足気味です。プラン見直しか目標msを下げてください。"
        )
    peak_kib = peak_memory_kib()
    if peak_kib is not None:
        print(f"(参考: このベンチ自身の最大RSS ≒ {peak_kib // 1024}MiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
