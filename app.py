import streamlit as st
import pandas as pd
import gspread
from datetime import date, timedelta
import json
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
import os
from openai import OpenAI
import io
from textwrap import wrap
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from pathlib import Path

SHEET_ID = "1UGc51y_ec9rzCGBAgx-xVeZVvjK3miNJwWaRpCe-IhI"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

@st.cache_resource
def get_gspread_client():
    """
    Google スプレッドシート用の gspread クライアントを取得する。

    優先順位：
    1. Streamlit Cloud の secrets: [gcp_service_account]
    2. 環境変数 GCP_SERVICE_ACCOUNT（JSON 文字列 or ファイルパス）
    3. ローカルの service_account.json ファイル
    """
    sa_info = None

    # ① Streamlit Cloud の secrets（推奨）
    try:
        if "gcp_service_account" in st.secrets:
            # st.secrets は MappingProxyType なので dict に変換
            sa_info = dict(st.secrets["gcp_service_account"])
    except Exception:
        # ローカル開発で secrets.toml が無いとき用に握りつぶす
        pass

    # ② 環境変数 GCP_SERVICE_ACCOUNT を JSON として読む
    if sa_info is None:
        sa_json = os.getenv("GCP_SERVICE_ACCOUNT")
        if sa_json:
            try:
                sa_info = json.loads(sa_json)
            except json.JSONDecodeError:
                # JSON じゃなければ「ファイルパス」とみなして読む
                if os.path.exists(sa_json):
                    creds = Credentials.from_service_account_file(
                        sa_json, scopes=SCOPES
                    )
                    return gspread.authorize(creds)
                else:
                    raise RuntimeError(
                        "環境変数 GCP_SERVICE_ACCOUNT が JSON でもファイルパスでもありません。"
                    )

    # ③ プロジェクト直下の service_account.json を読む（ローカル用）
    if sa_info is None and os.path.exists("service_account.json"):
        with open("service_account.json", "r", encoding="utf-8") as f:
            sa_info = json.load(f)

    # どれにも無ければエラー
    if sa_info is None:
        raise RuntimeError(
            "GCP_SERVICE_ACCOUNT が見つかりません。\n"
            "Streamlit Secrets の [gcp_service_account]、"
            "または環境変数 GCP_SERVICE_ACCOUNT、"
            "もしくは service_account.json を設定してください。"
        )

    # 共通：dict から Credentials を作成
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return gspread.authorize(creds)


# -----------------------------------
# 🔖 選択肢マスタ（上の方に配置）
# -----------------------------------
Phase_OPTIONS = [
    "Phase1-設計",
    "Phase2-構築",
    "Phase3-実装",
    "Phase4-仕上げ"
]

Category_OPTIONS = [
    "開業計画",
    "物件",
    "店舗工事",
    "メニュー計画",
    "スタッフ採用・教育",
    "販促営業活動",
    "備品関連",
    "管理データシステム構築",
    "営業準備",
    "試飲会レセプション"
]

Owner_OPTIONS = [
    "宮首(店長)",
    "副店長",
    "料理長",
    "松村さん(オーナー)",
    "まみさん(設計・デザイン)",
    "石川さん(コンサル)",
    "吉池さん",
    "スタッフ",
    "外部業者"
]

Status_OPTIONS = ["未着手", "進行中", "完了"]


# ===== OpenAI クライアント =====
# .env を読み込む
load_dotenv()
# OpenAI クライアント（API キーは .env から読み込み）
api_key = os.getenv("OPENAI_API_KEY")

if api_key is None or api_key.strip() == "":
    st.warning("⚠️ OPENAI_API_KEY が設定されていません。ガイドAIは制限モードになります。")
    client = None
else:
    client = OpenAI(api_key=api_key)


def ask_helper_bot(message: str, history: list[dict] | None = None) -> str:
    """LLM（OpenAI）に詳しい説明をさせる関数"""

    if client is None:
        return (
            "OpenAI API キーが設定されていないため、高度な説明モードは使えません。\n"
            "'.env' に OPENAI_API_KEY を設定してください。"
        )

    # 会話履歴の構築
    msgs = [
        {
            "role": "system",
            "content": (
                "あなたは『The Sake Council Tokyo タスク管理アプリ』の専門アシスタントです。\n"
                "Streamlit アプリの使い方、タスク項目の意味、関連する CSV ファイルについて、"
                "プロ向けにわかりやすく詳しく説明してください。"
            ),
        }
    ]

    # 過去の履歴を追加
    if history:
        msgs.extend(
            {"role": h["role"], "content": h["content"]}
            for h in history
            if h["role"] in ("user", "assistant")
        )

    msgs.append({"role": "user", "content": message})

    # OpenAI 呼び出し
    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=msgs,
        temperature=0.4,
    )

    return resp.choices[0].message.content


def make_pdf_from_markdown(text: str) -> bytes:
    """
    シンプルに Markdown テキストを PDF っぽいテキストPDFに変換する。
    デザインは簡素ですが、マニュアル配布用途には十分なレベル。
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    x_margin, y_margin = 40, 40
    y = height - y_margin

    for para in text.split("\n"):
        lines = wrap(para, 60) if para else [""]
        for line in lines:
            if y < y_margin:
                c.showPage()
                y = height - y_margin
            c.drawString(x_margin, y, line)
            y -= 14

    c.save()
    buf.seek(0)
    return buf.getvalue()

def helper_bot(message: str, history: list[dict] | None = None) -> str:
    """
    1. まず guide_bot_answer で「定型の質問」には即答
    2. 物足りないときだけ LLM に投げる
    3. OPENAI_API_KEY が無い場合は guide_bot_answer だけで動く
    """
    # ① ルールベースの即答
    rb = guide_bot_answer(message)

    # guide_bot の「デフォルト回答」には
    # 「もう少し具体的に質問してもらえれば…」の文言が入っている前提で判定
    if "もう少し具体的に質問してもらえれば" not in rb:
        return rb  # それなりに的を射た回答なので、そのまま返す

    # ② LLM が使えない環境なら、そのままデフォルト回答を返す
    if not os.environ.get("OPENAI_API_KEY"):
        return rb

    # ③ LLM に詳しく答えてもらう
    return ask_helper_bot(message, history)

# -------------- チャットボット（使い方ガイド） ---------------

def guide_bot_answer(message: str) -> str:
    """簡易ルールベースのガイドボット"""
    text = message.lower()

    if "ステータス" in message:
        return (
            "ステータスはタスクの進捗を表します。\n\n"
            "- **未着手**：まだ何も手を付けていないタスク\n"
            "- **進行中**：今まさに取り組んでいるタスク\n"
            "- **完了**：もう終わったタスク\n\n"
            "ガント・タスク一覧どちらもステータスに連動して表示されます。"
        )
    if "phase" in text or "フェーズ" in message:
        return (
            "Phase はタスクの大きな段階を表しています。\n\n"
            "- Phase1-設計：コンセプト決め・設計段階\n"
            "- Phase2-構築：仕組みやデータ作り・準備\n"
            "- Phase3-実装：実際の運用・撮影・導入など\n"
            "- Phase4-仕上げ：最終調整・テスト・オープン準備\n\n"
            "Phase ごとにガントとタスク一覧の行背景色が変わるようになっています。"
        )
    if "開始日" in message or "終了日" in message:
        return (
            "開始日・終了日は、そのタスクに取り組む期間です。\n\n"
            "1. **タスク一覧 → ✏️ 編集タブ** を開く\n"
            "2. 対象タスクの「開始日」「終了日」セルをクリックするとカレンダーが出ます\n"
            "3. 日付を選んで、画面下の **変更を保存** ボタンを押すと\n"
            "   - ガントチャート\n"
            "   - Google スプレッドシート\n"
            "   の両方に反映されます。"
        )
    if "フィルタ" in message or "filter" in text or "絞り込み" in message:
        return (
            "左サイドバーのフィルターで、表示するタスクを絞り込めます。\n\n"
            "- Phase：フェーズごとのタスクだけを見る\n"
            "- 担当：自分に関係するタスクだけを見る\n"
            "- ステータス：進行中だけをチェック、など\n\n"
            "複数条件を組み合わせることもできます。"
        )
    if "保存" in message or "google" in text or "スプレッドシート" in message:
        return (
            "画面下の **変更を保存** を押すと、編集した内容が\n"
            "そのまま Google スプレッドシートに上書き保存されます。\n\n"
            "保存される内容は主に：\n"
            "- タスク名・詳細・担当・ステータス\n"
            "- 開始日・終了日\n"
            "- 開始Day・終了Day（内部用）\n\n"
            "スプレッドシート側から直接編集した場合は、\n"
            "アプリを再読み込みすると最新状態が反映されます。"
        )
    if "追加" in message or "新しいタスク" in message or "登録" in message:
        return (
            "新しいタスクは画面一番下の **「新しいタスクを追加」フォーム** から登録します。\n\n"
            "1. Phase・カテゴリ・タスク名などを入力\n"
            "2. 開始日・終了日をカレンダーで指定\n"
            "3. ステータス・担当者を選ぶ\n"
            "4. 「追加」ボタンで登録\n\n"
            "追加後は自動でスプレッドシートにも反映されます。"
        )
    if "ガント" in message or "スケジュール" in message:
        return (
            "上部のガントチャートは、各タスクの開始日〜終了日を\n"
            "■（期間）・●（1日タスク）で表示しています。\n\n"
            "- 左側の「No.」「Phase」「タスク名」「担当」を見ながら\n"
            "  右側の日付のマスで期間感をざっくり把握する使い方です。\n"
            "- 日付を変更すると、自動でガントも更新されます。"
        )

    # デフォルト回答
    return (
        "このアプリでは、The Sake Council Tokyoのタスクを\n"
        "Googleスプレッドシートと連動して管理できます。\n\n"
        "よくある質問の例：\n"
        "- 「開始日と終了日の意味を教えて」\n"
        "- 「ステータスはどう使い分ける？」\n"
        "- 「フィルターの使い方」\n"
        "- 「新しいタスクの追加方法」\n\n"
        "もう少し具体的に質問してもらえれば、詳しく説明します！"
    )


# 進行スケジュール用の設定
PROJECT_START = date(2025, 11, 25)   # Day=1 の日付
BASE_END      = date(2026, 3, 31)   # デフォルト表示終了
MAX_SCHEDULE_DAYS = 180             # 最大表示日数（必要なら調整）

# 担当の候補一覧
ASSIGNEE_OPTIONS = ["店長", "副店長", "料理長", "オーナー","まみさん", "アルバイト", "サポーター", "全員", "その他"]

@st.cache_data(ttl=60)
def load_tasks():
    """Google スプレッドシートからタスク一覧を読み込む"""
    client = get_gspread_client()
    sh = client.open_by_key(SHEET_ID)
    ws = sh.sheet1
    data = ws.get_all_records()

    if not data:
        df = pd.DataFrame(columns=[
            "Day", "Phase", "カテゴリ", "タスク名", "詳細",
            "担当", "ステータス", "開始Day", "終了Day"
        ])
    else:
        df = pd.DataFrame(data)

    # ステータス補正
    df["ステータス"] = df["ステータス"].replace(["", None], "未着手")
    df["ステータス"] = df["ステータス"].fillna("未着手")

    # Day / 開始Day / 終了Day を数値化
    df["Day"] = pd.to_numeric(df.get("Day"), errors="coerce")

    if "開始Day" not in df.columns:
        df["開始Day"] = df["Day"]
    else:
        df["開始Day"] = pd.to_numeric(df["開始Day"], errors="coerce")

    if "終了Day" not in df.columns:
        df["終了Day"] = df["Day"]
    else:
        df["終了Day"] = pd.to_numeric(df["終了Day"], errors="coerce")

    # 欠損補完
    df["Day"] = df["Day"].fillna(1)
    df["開始Day"] = df["開始Day"].fillna(df["Day"])
    df["終了Day"] = df["終了Day"].fillna(df["開始Day"])

    # Day 系をクリップ
    for col in ["Day", "開始Day", "終了Day"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(1)
        df[col] = df[col].clip(lower=1, upper=MAX_SCHEDULE_DAYS)

    # 開始日 / 終了日（datetime64）を用意
    if "開始日" in df.columns:
        df["開始日"] = pd.to_datetime(df["開始日"], errors="coerce")
    else:
        df["開始日"] = pd.to_datetime(PROJECT_START) + pd.to_timedelta(df["開始Day"] - 1, unit="D")

    if "終了日" in df.columns:
        df["終了日"] = pd.to_datetime(df["終了日"], errors="coerce")
    else:
        df["終了日"] = df["開始日"]

    # 🔽 読み込み直後に型をそろえる
    df["Phase"] = df["Phase"].astype(str)
    df["タスク名"] = df["タスク名"].astype(str)

    return df

CATEGORY_BASE_STYLE = {
    "開業計画": "background-color: rgba(46, 134, 193, 0.30);",     # 濃い水色
    "物件": "background-color: rgba(39, 174, 96, 0.30);",           # 緑
    "店舗工事": "background-color: rgba(142, 68, 173, 0.30);",     # 紫
    "メニュー計画": "background-color: rgba(241, 196, 15, 0.35);", # 黄色
    "スタッフ採用・教育": "background-color: rgba(231, 76, 60, 0.35);", # 赤
    "販促営業活動": "background-color: rgba(52, 152, 219, 0.35);",  # ブルー
    "備品関連": "background-color: rgba(243, 156, 18, 0.35);",     # オレンジ
    "管理データシステム構築": "background-color: rgba(26, 188, 156, 0.35);", # ティール
    "営業準備": "background-color: rgba(127, 140, 141, 0.35);",   # グレー
    "試飲会レセプション": "background-color: rgba(155, 89, 182, 0.35);", # 明るい紫
}

def style_row(row):
    """
    カテゴリ と ステータス、さらに『ログイン日より前に終わっているか』で
    行の見た目を決める。
    """

    # --- カテゴリベースの色分け ---
    category = str(row.get("カテゴリ", "")).strip()
    base = CATEGORY_BASE_STYLE.get(category, "")  # 未定義カテゴリはデフォルト（無色）

    # --- ステータス取得 ---
    status = str(row.get("ステータス", "")).lower()

    # --- 終了日がログイン日より前なら「過去タスク」とみなす ---
    end_val = row.get("終了日", None)
    is_past = False

    if pd.notna(end_val):
        # pandas.Timestamp / datetime / date / 文字列 どれでもOKにする
        try:
            end_date = pd.to_datetime(end_val).date()
            if end_date < LOGIN_DATE:
                is_past = True
        except Exception:
            pass

    # --- 完了タスクは左に緑線で強調 ---
    if status == "完了":
        base += " border-left: 4px solid #2ecc71;"

    # --- 過去タスクは半透明＆文字色を薄く ---
    if is_past:
        base += " opacity: 0.45; color: #bbbbbb;"

    return [base] * len(row)


def fade_past_days(df: pd.DataFrame) -> pd.DataFrame:
    """
    ガントチャート用：
    今日より前の日付列だけ背景を少し暗くする
    """
    today = date.today()

    # 日付以外の固定列
    fixed_cols = ["No.", "Phase", "カテゴリ", "タスク名", "担当", "開始日", "終了日"]

    # 全セル分のスタイル DataFrame を作る
    styles = pd.DataFrame("", index=df.index, columns=df.columns)

    for col in df.columns:
        if col in fixed_cols:
            # 固定カラムは何もしない
            continue

        # 列名 "11/25" などを 日付 に変換（年は PROJECT_START の年を使う）
        try:
            col_date = datetime.strptime(col, "%m/%d").date().replace(year=PROJECT_START.year)
        except ValueError:
            # 日付っぽくない列名はスキップ
            continue

        if col_date < today:
            # 今日より前の列だけ少し暗く
            styles[col] = "background-color: rgba(150, 150, 150, 0.25);"
        else:
            styles[col] = ""

    return styles


def highlight_status(row):
    status = row.get("ステータス", "")
    if status == "完了":
        return ['background-color: rgba(46, 204, 113, 0.12)'] * len(row)
    return [''] * len(row)


def decorate_status(df):
    df = df.copy()
    df["ステータス表示"] = df["ステータス"].apply(
        lambda s: "🟢 完了" if s == "完了"
        else "🟡 進行中" if s == "進行中"
        else "⚪ 未着手"
    )
    return df


@st.cache_data
def build_schedule_table(df: pd.DataFrame) -> pd.DataFrame:
    """開始日/終了日 から、No. 付きガント風スケジュール表を作成"""
    if df.empty:
        return pd.DataFrame()

    df = df.copy()

    # 文字列にそろえる
    df["Phase"] = df["Phase"].astype(str)
    df["タスク名"] = df["タスク名"].astype(str)

    # 日付を date にそろえる
    df["開始日"] = pd.to_datetime(df["開始日"], errors="coerce").dt.date
    df["終了日"] = pd.to_datetime(df["終了日"], errors="coerce").dt.date

    # 欠損の補正
    df["開始日"] = df["開始日"].fillna(PROJECT_START)
    df["終了日"] = df["終了日"].fillna(df["開始日"])

    mask = df["終了日"] < df["開始日"]
    df.loc[mask, "終了日"] = df.loc[mask, "開始日"]

    # Day 相当を作成
    df["開始Day"] = (df["開始日"] - PROJECT_START).apply(lambda x: x.days + 1)
    df["終了Day"] = (df["終了日"] - PROJECT_START).apply(lambda x: x.days + 1)

    df["開始Day"] = df["開始Day"].clip(1, MAX_SCHEDULE_DAYS)
    df["終了Day"] = df["終了Day"].clip(1, MAX_SCHEDULE_DAYS)

    max_end_day = int(df["終了Day"].max())
    max_end_day = min(max_end_day, MAX_SCHEDULE_DAYS)

    dynamic_end = max(
        BASE_END,
        PROJECT_START + timedelta(days=max_end_day - 1)
    )

    num_days = (dynamic_end - PROJECT_START).days + 1
    date_list = [PROJECT_START + timedelta(days=i) for i in range(num_days)]
    date_labels = [d.strftime("%m/%d") for d in date_list]

    # 並び順を決めて No. を振る
    df = df.sort_values(["開始日", "Phase", "タスク名"]).reset_index(drop=True)
    df["No."] = df.index + 1

    rows = []
    for _, row in df.iterrows():
        start_day = int(row["開始Day"])
        end_day = int(row["終了Day"])
        start_idx = start_day - 1
        end_idx = end_day - 1

        # ★ ステータスを取得（該当なければ空文字）
        status = str(row.get("ステータス", "")).strip()

        row_data = {
            "No.": int(row["No."]),
            "Phase": row.get("Phase", ""),
            "カテゴリ": row.get("カテゴリ", ""),
            "タスク名": row.get("タスク名", ""),
            "担当": row.get("担当", ""),
            "ステータス": status,   # ★ ステータス列もガントに表示
            "開始日": row["開始日"],
            "終了日": row["終了日"],
        }

        for idx, label in enumerate(date_labels):
            if start_idx <= idx <= end_idx:
                # ★ ステータスに応じて記号を切り替え
                if status == "完了":
                    mark = "✔"                  # 完了タスクは期間中ずっとチェック
                else:
                    mark = "●" if start_day == end_day else "■"  # 従来仕様
                row_data[label] = mark
            else:
                row_data[label] = ""

        rows.append(row_data)

    sched_df = pd.DataFrame(rows)

    fixed = ["No.", "Phase", "カテゴリ", "タスク名", "担当", "ステータス", "開始日", "終了日"]
    others = [c for c in sched_df.columns if c not in fixed]
    return sched_df[fixed + others]

@st.cache_data
def build_schedule_table_weekly(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["開始日"] = pd.to_datetime(df["開始日"]).dt.date
    df["終了日"] = pd.to_datetime(df["終了日"]).dt.date

    df["開始日"] = df["開始日"].fillna(PROJECT_START)
    df["終了日"] = df["終了日"].fillna(df["開始日"])

    df["開始Week"] = df["開始日"].apply(lambda d: (d - PROJECT_START).days // 7 + 1)
    df["終了Week"] = df["終了日"].apply(lambda d: (d - PROJECT_START).days // 7 + 1)

    df = df.sort_values(["開始Week", "Phase"]).reset_index(drop=True)
    df["No."] = df.index + 1

    max_week = int(df["終了Week"].max())

    # ▼ 週表示：11月4週目、12月1週目… にする
    week_labels = []
    for w in range(max_week):
        week_start = PROJECT_START + timedelta(days=w * 7)  # 週の開始日
        month = week_start.month
        # ▼ 月内の週番号：1〜5
        week_of_month = (week_start.day - 1) // 7 + 1
        label = f"{month}月{week_of_month}週目"
        week_labels.append(label)

    rows = []
    for _, row in df.iterrows():
        start_w = int(row["開始Week"])
        end_w = int(row["終了Week"])

        status = str(row.get("ステータス", "")).strip()
        mark = "✔" if status == "完了" else "■"

        row_data = {
            "No.": row["No."],
            "Phase": row["Phase"],
            "カテゴリ": row["カテゴリ"],
            "タスク名": row["タスク名"],
            "担当": row["担当"],
            "ステータス": status,
            "開始日": row["開始日"],
            "終了日": row["終了日"],
        }

        for w, label in enumerate(week_labels):
            row_data[label] = mark if (start_w - 1) <= w <= (end_w - 1) else ""

        rows.append(row_data)

    sched_df = pd.DataFrame(rows)
    fixed = ["No.", "Phase", "カテゴリ", "タスク名", "担当", "ステータス", "開始日", "終了日"]
    others = [c for c in sched_df.columns if c not in fixed]
    return sched_df[fixed + others]


def save_tasks(df: pd.DataFrame):
    """DataFrame全体をスプレッドシートに書き戻す（全上書き方式）"""
    client = get_gspread_client()
    sh = client.open_by_key(SHEET_ID)
    ws = sh.sheet1

    # ✅ No. はシートには出さない（表示専用カラム）
    save_df = df.copy()
    if "No." in save_df.columns:
        save_df = save_df.drop(columns=["No."])

    header = list(save_df.columns)
    values = [header] + save_df.astype(str).values.tolist()

    ws.clear()
    ws.update("A1", values)


# ------------------ ここから画面本体 ---------------------

from datetime import datetime, date, timedelta

st.set_page_config(page_title="The Sake Council Tokyo タスク管理", layout="wide")

# 👇 このセッションでの「ログイン日」を 1 回だけ記録
if "login_date" not in st.session_state:
    st.session_state["login_date"] = date.today()

LOGIN_DATE = st.session_state["login_date"]


st.title("The Sake Council Tokyo タスク管理（Googleスプレッドシート共有版）")

# --- データ読み込み（セッション管理） ---
if "df" not in st.session_state:
    st.session_state["df"] = load_tasks()

df = st.session_state["df"]

# --- ログイン的な役割選択 ---
st.sidebar.header("自分の役割")
current_user = st.sidebar.selectbox(
    "ログイン名（役割）",
    ["店長", "副店長", "料理長", "オーナー", "まみさん", "アルバイト", "その他"],
)

# =========================
# 🔍 フィルター
# =========================
st.sidebar.header("フィルター")
phase_filter = st.sidebar.multiselect("Phase", sorted(df["Phase"].dropna().unique()))
# ▼ 担当者名を単体ごとにバラして候補を作る
all_owner_strings = df["担当"].dropna().astype(str)

names = set()
for s in all_owner_strings:
    # 区切り文字を一旦カンマに統一（, ・ 、 ・ ／ などを想定）
    normalized = (
        s.replace("、", ",")
         .replace("，", ",")
         .replace("／", ",")
         .replace("/", ",")
    )
    for part in normalized.split(","):
        name = part.strip()
        if name:
            names.add(name)

owner_options = sorted(names)

owner_filter = st.sidebar.multiselect("担当", owner_options)

status_filter = st.sidebar.multiselect("ステータス", sorted(df["ステータス"].dropna().unique()))

# ---------- サイドバー：ヘルプ & data フォルダ管理 ----------

st.sidebar.markdown("### ❓ このアプリやCSVの使い方")

# ① PDF風マニュアルの自動生成
with st.sidebar.expander("📘 このアプリのPDF風マニュアルを自動生成する"):

    st.write(
        "「マニュアルを生成」ボタンを押すと、"
        "このタスク管理アプリの使い方と、主要なCSVファイルの役割をまとめた説明文をAIが作成します。"
    )

    if st.button("マニュアルを生成", key="btn_make_manual_sidebar"):
        with st.spinner("マニュアルを作成中…"):
            # ここは今まで使っていた ask_helper_bot をそのまま利用
            prompt = (
                "恵比寿日本酒プロジェクトのタスク管理アプリの使い方と、"
                "data フォルダ内の主な CSV（例：cooking_menu_list.csv、sake_wine_list.csv など）の役割を、"
                "スタッフ向けマニュアルとして日本語でわかりやすく説明してください。"
            )
            manual_text = ask_helper_bot(prompt, history=[])
        st.session_state["manual_text"] = manual_text
        st.success("マニュアルを生成しました。メイン画面の下部に表示されます。")

# ② ヘルプチャット（簡易QA）
with st.sidebar.expander("💬 ヘルプチャットボット"):

    st.write("このアプリの使い方や、タスク／CSV の意味などを質問できます。")

    help_q = st.text_input(
        "質問を入力してください",
        value="",
        key="sidebar_help_question",
        placeholder="例）開始日と終了日はどう使い分ける？",
    )

    if st.button("質問する", key="sidebar_help_ask"):
        if help_q.strip():
            answer = guide_bot_answer(help_q)
            st.session_state["sidebar_help_answer"] = answer
        else:
            st.session_state["sidebar_help_answer"] = "質問文を入力してください。"

    if "sidebar_help_answer" in st.session_state:
        st.markdown("---")
        st.markdown("**回答**")
        st.markdown(st.session_state["sidebar_help_answer"])

# ③ data フォルダの CSV 管理
with st.sidebar.expander("📁 data フォルダの管理"):

    data_dir = Path(__file__).parent / "data"

    if not data_dir.exists():
        st.info("同ディレクトリに data フォルダが見つかりません。")
    else:
        # data/ 以下の CSV ファイル一覧
        csv_files = sorted(
            [f.name for f in data_dir.iterdir() if f.is_file() and f.suffix == ".csv"]
        )

        if not csv_files:
            st.info("data フォルダ内に CSV ファイルがありません。")
        else:
            selected_csv = st.selectbox("CSVファイルを選択", csv_files)

            if st.button("このCSVの中身をプレビュー", key="btn_preview_csv"):
                try:
                    df_preview = pd.read_csv(data_dir / selected_csv)
                    # 先頭数行だけを表示用にセッションへ渡す
                    st.session_state["data_preview_name"] = selected_csv
                    st.session_state["data_preview_df"] = df_preview.head(20)
                    st.success("メイン画面にプレビューを表示しました。")
                except Exception as e:
                    st.error("CSVの読み込みに失敗しました。")
                    st.code(str(e))

            if st.button("このCSVの役割を説明して", key="btn_explain_csv"):
                # 列名だけ取得して説明に渡す
                try:
                    df_tmp = pd.read_csv(data_dir / selected_csv, nrows=3)
                    cols = ", ".join(df_tmp.columns.tolist())
                    prompt = (
                        f"次のCSVファイルの用途と、主なカラムの意味を日本語で説明してください。\n\n"
                        f"ファイル名: {selected_csv}\n"
                        f"カラム: {cols}\n\n"
                        "このプロジェクトは、恵比寿の日本酒バー開業に向けたタスク／原価管理アプリです。"
                    )
                    explanation = ask_helper_bot(prompt, history=[])
                    st.session_state["data_csv_explanation"] = explanation
                    st.success("説明を生成しました。メイン画面の下部に表示されます。")
                except Exception as e:
                    st.error("CSVの読み込みに失敗しました。")
                    st.code(str(e))


# --- フィルター適用後の view_df を作るところを修正 ---

view_df = df.copy()

if phase_filter:
    view_df = view_df[view_df["Phase"].isin(phase_filter)]

if owner_filter:
    # 「担当」文字列に、選択した名前が1つでも含まれていれば True
    owner_col = view_df["担当"].fillna("").astype(str)

    mask_owner = pd.Series(False, index=view_df.index)
    for name in owner_filter:
        # 完全一致じゃなく「含まれる」でOKなら contains で十分
        mask_owner |= owner_col.str.contains(name)

    view_df = view_df[mask_owner]

if status_filter:
    view_df = view_df[view_df["ステータス"].isin(status_filter)]


# ------- 表示用の並び替え & No. 付与（型を安全にそろえる） -------

# 開始日は一度 datetime にそろえる
if "開始日" in view_df.columns:
    view_df["開始日"] = pd.to_datetime(view_df["開始日"], errors="coerce")

# Phase / タスク名 は必ず文字列にしておく（Categoricalトラブル回避）
for col in ["Phase", "タスク名"]:
    if col in view_df.columns:
        view_df[col] = view_df[col].astype(str)

# 並び替え → インデックス振り直し
view_df = view_df.sort_values(["開始日", "Phase", "タスク名"]).reset_index(drop=True)

# 画面用の連番 No.
view_df["No."] = view_df.index + 1


# =========================
# 📆 進行スケジュール
# =========================
st.subheader("📆 進行スケジュール（11月末〜3月＋延長）")

show_schedule = st.checkbox("スケジュール表を表示する", value=True)

if show_schedule:
    # 👇 ここに表示単位セレクトを入れる
    view_mode = st.selectbox("スケジュール表示単位", ["日次", "週次"], index=0)

    # フィルター後のデータを使う
    selected_df = view_df if not view_df.empty else df

    # 日次 or 週次 で作るテーブルを切り替え
    if view_mode == "日次":
        schedule_df = build_schedule_table(selected_df)
    else:
        schedule_df = build_schedule_table_weekly(selected_df)

    if schedule_df.empty:
        st.info("対象タスクが未登録のため、スケジュール表を表示できません。")
    else:
        st.caption("横スクロールで進行状況を確認できます。")

        # スタイル適用（過去日フェードは「日次」のときだけ適用）
        if view_mode == "日次":
            styled_schedule = (
                schedule_df
                .style
                .apply(style_row, axis=1)          # 行ごとの Phase 色
                .apply(fade_past_days, axis=None)  # 過去列を暗く
            )
        else:
            styled_schedule = (
                schedule_df
                .style
                .apply(style_row, axis=1)          # 行ごとの Phase 色のみ
            )

        st.dataframe(
            styled_schedule,
            use_container_width=True,
            hide_index=True,
        )

st.divider()


# =========================
# 📋 タスク一覧
# =========================
st.subheader("タスク一覧")

list_df = view_df.copy().reset_index()
list_df.rename(columns={"index": "_orig_index"}, inplace=True)

# 表示用 No. を毎回付け直す
if "No." in list_df.columns:
    list_df = list_df.drop(columns=["No."])
list_df.insert(0, "No.", range(1, len(list_df) + 1))

view_df = view_df.reset_index(drop=True)
view_df["No."] = view_df.index + 1

if len(view_df) == 0:
    st.info("条件に一致するタスクがありません。")
else:
    tab1, tab2 = st.tabs(["👀 一覧（色付き）", "✏️ 編集"])

    # 一覧タブ
    with tab1:
        styled_df = decorate_status(view_df)

        hidden_cols = ["ステータス", "開始Day", "終了Day", "Day"]
        display_cols = ["No.", "ステータス表示"] + [
            c
            for c in styled_df.columns
            if c not in hidden_cols + ["ステータス表示", "No."]
        ]
        styled_df = styled_df[display_cols]

        # 🔽 ここを追加：日付列は文字列にそろえる（pyarrow 対策）
        for col in ["開始日", "終了日"]:
            if col in styled_df.columns:
                styled_df[col] = pd.to_datetime(styled_df[col], errors="coerce").dt.strftime("%Y-%m-%d")

        # そのあとスタイル適用
        styled = styled_df.style.apply(style_row, axis=1)
        st.dataframe(styled, use_container_width=True, hide_index=True)

    # 編集タブ
    with tab2:
        st.caption("ここでタスク内容を編集できます（保存するとシートに反映）")

        editable_df = list_df.copy()

        # 日付整形
        editable_df["開始日"] = pd.to_datetime(editable_df["開始日"], errors="coerce")
        editable_df["終了日"] = pd.to_datetime(editable_df["終了日"], errors="coerce")

        # Day 系は編集画面から隠す
        edit_cols = [c for c in editable_df.columns if c not in ["Day", "開始Day", "終了Day"]]
        editable_df = editable_df[edit_cols]

        edited_df = st.data_editor(
            editable_df,
            column_config={
                "No.": st.column_config.NumberColumn("No.", disabled=True, width="small"),
                "ステータス": st.column_config.SelectboxColumn(
                    "ステータス",
                    options=["未着手", "進行中", "完了"],
                    required=True,
                    width="small",
                ),
                "開始日": st.column_config.DateColumn("開始日", format="YYYY-MM-DD", width="medium"),
                "終了日": st.column_config.DateColumn("終了日", format="YYYY-MM-DD", width="medium"),
            },
            use_container_width=True,
            hide_index=True,
            key="task_editor",
        )

        # ↓↓↓ 変更を保存ボタンの中で、_orig_index を使って df を更新するようにする
        if st.button("変更を保存", type="primary"):
            base_df = st.session_state["df"].copy()

            for _, row in edited_df.iterrows():
                orig_idx = int(row["_orig_index"])
                # 日付 → Day に変換
                for day_col, date_col in [("開始Day", "開始日"), ("終了Day", "終了日")]:
                    d = pd.to_datetime(row[date_col], errors="coerce")
                    if pd.isna(d):
                        day_val = 1
                    else:
                        day_val = (d.date() - PROJECT_START).days + 1
                    base_df.at[orig_idx, day_col] = max(1, min(MAX_SCHEDULE_DAYS, day_val))

                # その他の項目も上書き
                for col in ["Phase", "カテゴリ", "タスク名", "詳細", "担当", "ステータス", "開始日", "終了日"]:
                    if col in base_df.columns and col in edited_df.columns:
                        base_df.at[orig_idx, col] = row[col]

            st.session_state["df"] = base_df
            save_tasks(base_df)
            st.success("Googleスプレッドシートに保存しました ✅")
            st.rerun()


st.markdown("### 🗑 タスクを削除")

delete_options = {
    f"{int(row['No.'])}: {row['タスク名']}（{row['担当']}）": int(row["_orig_index"])
    for _, row in list_df.iterrows()
}

if delete_options:
    delete_label = st.selectbox("削除するタスクを選択", list(delete_options.keys()))
    target_idx = delete_options[delete_label]

    if st.button("このタスクを削除する", type="secondary"):
        base_df = st.session_state["df"].copy()
        base_df = base_df.drop(index=target_idx).reset_index(drop=True)
        st.session_state["df"] = base_df
        save_tasks(base_df)
        st.success("タスクを削除しました ✅")
        st.rerun()
else:
    st.info("削除できるタスクがありません。")


st.divider()

# =========================
# ✨ 新しいタスクを追加 (Improved UI)
# =========================

st.markdown("## 🆕 新しいタスクを追加")
st.caption("必要な情報を入力して登録してください")

with st.container():
    st.markdown("""
    <style>
    /* フォーム全体の視認性UP */
    .task-form-box {
        padding: 18px 20px;
        border-radius: 10px;
        background-color: rgba(255,255,255,0.05);
        border: 1px solid rgba(255,255,255,0.08);
        margin-bottom: 20px;
    }
    </style>
    """, unsafe_allow_html=True)

    with st.form(key="new_task"):
        st.markdown('<div class="task-form-box">', unsafe_allow_html=True)

        col1, col2, col3 = st.columns([1.2, 1, 1])

        with col1:
            phase = st.selectbox("Phase", Phase_OPTIONS, index=0)
            category = st.selectbox("カテゴリ", Category_OPTIONS)
            title = st.text_input("タスク名（※必須）")

        with col2:
            owner = st.selectbox("担当", Owner_OPTIONS)
            status = st.selectbox("ステータス", Status_OPTIONS)

        with col3:
            start_date = st.date_input("開始日", value=date.today())
            end_date = st.date_input("終了日", value=date.today())

        detail = st.text_area("詳細（任意）", placeholder="補足があれば記入")

        st.markdown('</div>', unsafe_allow_html=True)

        submitted = st.form_submit_button("➕ タスクを追加")

        if submitted:
            if not title:
                st.error("⚠️ タスク名が未入力です")
            else:
                if end_date < start_date:
                    end_date = start_date

                start_day = (start_date - PROJECT_START).days + 1
                end_day = (end_date - PROJECT_START).days + 1

                new_row = {
                    "Day": start_day,
                    "Phase": phase,
                    "カテゴリ": category,
                    "タスク名": title,
                    "詳細": detail,
                    "担当": owner,
                    "ステータス": status,
                    "開始Day": start_day,
                    "終了Day": end_day,
                    "開始日": start_date,
                    "終了日": end_date,
                }

                st.session_state["df"] = pd.concat(
                    [st.session_state["df"], pd.DataFrame([new_row])],
                    ignore_index=True
                )
                save_tasks(st.session_state["df"])
                st.success("✨ タスクを追加しました！")
                st.rerun()

# =========================
# 📘 生成されたマニュアル表示
# =========================
if "manual_text" in st.session_state:
    st.divider()
    st.subheader("📘 このアプリの使い方マニュアル（AI生成）")
    st.markdown(st.session_state["manual_text"])

# =========================
# 📁 data フォルダ CSV のプレビュー
# =========================
if "data_preview_df" in st.session_state:
    st.divider()
    name = st.session_state.get("data_preview_name", "選択されたCSV")
    st.subheader(f"📁 data/{name} のプレビュー（先頭20行）")
    st.dataframe(st.session_state["data_preview_df"], use_container_width=True)

if "data_csv_explanation" in st.session_state:
    st.markdown("#### 📄 選択したCSVファイルの役割・カラム説明（AI回答）")
    st.markdown(st.session_state["data_csv_explanation"])

