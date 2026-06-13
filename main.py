"""
医学論文自動収集・要約システム メインスクリプト

PubMed検索 → フィルタリング → AI要約 → Word出力 の
パイプラインをオーケストレーションする。
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
import os

from pubmed_searcher import PubMedSearcher
from paper_filter import PaperFilter
from ai_summarizer import AISummarizer
from word_generator import WordGenerator

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            "paper_collector.log", encoding="utf-8", mode="a"
        )
    ]
)
logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yaml") -> dict:
    """設定ファイルを読み込む"""
    path = Path(config_path)
    if not path.exists():
        logger.error(f"設定ファイルが見つかりません: {config_path}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logger.info(f"設定ファイルを読み込みました: {config_path}")
    return config


def main():
    """メインエントリーポイント"""
    # コマンドライン引数
    parser = argparse.ArgumentParser(
        description="医学論文自動収集・要約システム"
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="設定ファイルのパス（デフォルト: config.yaml）"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="AI要約をスキップして検索・フィルタリングのみ実行"
    )
    parser.add_argument(
        "--weeks-back", type=int, default=None,
        help="何週間前まで検索するか（デフォルト: 設定ファイルの値）"
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="出力ディレクトリ（デフォルト: 設定ファイルの値）"
    )
    args = parser.parse_args()

    # 環境変数の読み込み
    load_dotenv()

    # 設定読み込み
    # スクリプトのあるディレクトリを基準にする
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    config = load_config(args.config)

    # 曜日別テーマの動的適用
    today = datetime.now()
    weekday_name = today.strftime("%A")  # 'Monday', 'Tuesday', etc.
    
    daily_themes = config.get("daily_themes", {})
    if weekday_name in daily_themes:
        theme = daily_themes[weekday_name]
        logger.info(f"本日のテーマ ({weekday_name}) を適用します。")
        
        # 分野の上書き (該当曜日の分野のみを検索対象とする)
        if "specialties" not in config:
            config["specialties"] = {}
        config["specialties"]["primary"] = theme.get("specialties", [])
        config["specialties"]["secondary"] = []
        
        # ジャーナルの上書き (テーマ特化雑誌 + Tier1総合誌のみに絞る)
        if "journals" not in config:
            config["journals"] = {}
        tier1 = config["journals"].get("tier1", [])
        theme_journals = theme.get("journals", [])
        config["journals"]["tier1"] = theme_journals + tier1
        # config["journals"]["tier2"] = []  # クエリ肥大化を防ぐためクリア (削除: Tier2以下も含めるため)
        
        # 抽出件数を5件に変更
        if "search" not in config:
            config["search"] = {}
        config["search"]["top_n"] = 5
        config["search"]["detailed_top_n"] = 5
        
        logger.info(f"テーマ分野: {config['specialties']['primary']}")
        logger.info(f"優先ジャーナル: {config['journals']['tier1']}")

    # APIキーの確認
    gemini_key = os.getenv("GEMINI_API_KEY")
    ncbi_email = os.getenv("NCBI_EMAIL", "user@example.com")
    ncbi_api_key = os.getenv("NCBI_API_KEY")

    if not gemini_key and not args.dry_run:
        logger.error(
            "GEMINI_API_KEY が設定されていません。"
            ".envファイルを確認してください。"
        )
        sys.exit(1)

    # 検索期間の計算
    days_back = config.get("search", {}).get("days_back", 7)
    if args.weeks_back:
        days_back = args.weeks_back * 7

    logger.info("=" * 60)
    logger.info("医学論文自動収集・要約システム 実行開始")
    logger.info(f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"検索期間: 過去{days_back}日間")
    logger.info(f"ドライラン: {'はい' if args.dry_run else 'いいえ'}")
    logger.info("=" * 60)

    try:
        # ステップ1: PubMed検索
        logger.info("━━━ ステップ1: PubMed検索 ━━━")
        searcher = PubMedSearcher(config, ncbi_email, ncbi_api_key)
        papers = searcher.search(days_back=days_back)

        if not papers:
            logger.warning("論文が見つかりませんでした。処理を終了します。")
            return

        # ステップ2: フィルタリング・優先度付け
        logger.info("━━━ ステップ2: フィルタリング・優先度付け ━━━")
        filterer = PaperFilter(config)
        top_papers = filterer.filter_and_rank(papers)

        if not top_papers:
            logger.warning(
                "条件に合う論文がありませんでした。処理を終了します。"
            )
            return

        # 各論文の選出理由を表示
        logger.info("選出された論文:")
        for paper in top_papers:
            logger.info(
                f"  #{paper.priority_rank} "
                f"[スコア:{paper.priority_score:.1f}] "
                f"{paper.title[:60]}..."
            )

        if args.dry_run:
            logger.info("ドライランモード: AI要約とWord出力をスキップします")
            logger.info("━━━ ドライラン完了 ━━━")

            # 検索結果サマリーを表示
            for paper in top_papers:
                logger.info(f"\n--- #{paper.priority_rank} ---")
                logger.info(f"タイトル: {paper.title}")
                logger.info(f"ジャーナル: {paper.journal}")
                logger.info(f"タイプ: {', '.join(paper.pub_types)}")
                logger.info(f"DOI: {paper.doi}")
                logger.info(f"スコア: {paper.priority_score:.1f}")
            return

        # ステップ3: AI要約
        logger.info("━━━ ステップ3: AI要約生成 ━━━")
        summarizer = AISummarizer(config, gemini_key)

        # 選出理由を事前生成
        for paper in top_papers:
            paper._selection_reason = summarizer.generate_selection_reason(
                paper
            )

        top_papers = summarizer.summarize_papers(
            top_papers,
            detailed_top_n=config.get("search", {}).get("detailed_top_n", 3)
        )

        # ステップ4: Word文書生成
        logger.info("━━━ ステップ4: Word文書生成 ━━━")
        generator = WordGenerator(config)

        output_path = None
        if args.output_dir:
            output_dir = Path(args.output_dir)
            output_dir.mkdir(exist_ok=True)
            date_str = datetime.now().strftime("%Y%m%d")
            output_path = str(
                output_dir / f"週刊医学論文レビュー_{date_str}.docx"
            )

        result_path = generator.generate(top_papers, output_path)

        # ステップ5: 履歴更新
        logger.info("━━━ ステップ5: 履歴更新 ━━━")
        filterer.save_history(top_papers)

        logger.info("=" * 60)
        logger.info("[SUCCESS] 全処理が完了しました")
        logger.info(f"出力ファイル: {result_path}")
        logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.info("ユーザーにより中断されました")
        sys.exit(0)
    except Exception as e:
        logger.error(f"予期しないエラーが発生しました: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
