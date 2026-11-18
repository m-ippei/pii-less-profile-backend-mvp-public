"""HTTP 共通処理の置き場（design/02-architecture.md §3）。

`main.py` は「結線（Principal解決、Router、ミドルウェア）」を担う。全エンドポイントが
必ず通る共通処理をここに集約し、FastAPI 依存を `shared/web`（フレームワーク非依存）へ
持ち込まない。
"""
