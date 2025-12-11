#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
社内NW端末 Ping疎通確認ツール
継続的にPing監視を実行し、結果を表示します。
"""

import subprocess
import threading
import time
import platform
import json
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path


class PingMonitor:
    def __init__(self, hosts_file="hosts.txt", interval=5, config_file="config.json"):
        """
        Args:
            hosts_file: 監視対象ホストリストファイル
            interval: Ping実行間隔（秒）
            config_file: 設定ファイル
        """
        self.hosts_file = hosts_file
        self.interval = interval
        self.config_file = config_file
        self.hosts = []
        self.status = {}
        self.previous_status = {}  # 前回の状態を保持（状態変化検知用）
        self.consecutive_failures = {}  # 連続NG回数（ホストごと）
        self.mention_sent = {}  # メンション送信済みフラグ（ホストごと）
        self.lock = threading.Lock()
        self.running = False
        self.config = self.load_config()

    def load_config(self):
        """設定ファイルを読み込む"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            print(f"[INFO] 設定ファイル '{self.config_file}' を読み込みました")
            return config
        except FileNotFoundError:
            print(f"[WARN] 設定ファイル '{self.config_file}' が見つかりません（Slack通知は無効）")
            return {"slack": {"enabled": False}}
        except json.JSONDecodeError as e:
            print(f"[WARN] 設定ファイルの形式エラー: {e}（Slack通知は無効）")
            return {"slack": {"enabled": False}}
        except Exception as e:
            print(f"[WARN] 設定ファイル読み込みエラー: {e}（Slack通知は無効）")
            return {"slack": {"enabled": False}}

    def send_slack_notification(self, host, is_alive, timestamp, include_mention=False, failure_count=0):
        """
        Slackに通知を送信

        Args:
            host: 対象ホスト
            is_alive: 現在の状態（True: OK, False: NG）
            timestamp: タイムスタンプ
            include_mention: メンションを含めるか（初回ダウン通知のみTrue）
            failure_count: 連続失敗回数
        """
        slack_config = self.config.get("slack", {})

        if not slack_config.get("enabled", False):
            return

        webhook_url = slack_config.get("webhook_url", "")
        if not webhook_url or webhook_url.startswith("https://hooks.slack.com/services/YOUR"):
            return  # Webhook URLが未設定

        # 状態に応じた通知メッセージ作成
        if not is_alive:
            # ダウン通知
            if not slack_config.get("notify_on_down", True):
                return
            color = "danger"
            status_emoji = ":x:"
            status_text = f"NG (疎通不可) - {failure_count}回連続失敗"
            title = f"{status_emoji} ホストダウン検知"
        else:
            # 復旧通知
            if not slack_config.get("notify_on_recovery", True):
                return
            color = "good"
            status_emoji = ":white_check_mark:"
            status_text = "OK (疎通成功)"
            title = f"{status_emoji} ホスト復旧"

        # メンション文字列を構築
        mention_text = ""
        if include_mention:
            mentions = []
            # ユーザーグループのメンション
            for group_id in slack_config.get("mention_groups", []):
                mentions.append(f"<!subteam^{group_id}>")
            # ユーザーのメンション
            for user_id in slack_config.get("mention_users", []):
                mentions.append(f"<@{user_id}>")

            if mentions:
                mention_text = " ".join(mentions) + "\n"

        # Slackメッセージペイロード作成
        payload = {
            "username": slack_config.get("username", "Ping監視Bot"),
            "text": mention_text if mention_text else None,  # メンションはtextフィールドに含める
            "attachments": [{
                "color": color,
                "title": title,
                "fields": [
                    {
                        "title": "ホスト",
                        "value": host,
                        "short": True
                    },
                    {
                        "title": "状態",
                        "value": status_text,
                        "short": True
                    },
                    {
                        "title": "検知時刻",
                        "value": timestamp,
                        "short": False
                    }
                ],
                "footer": "社内NW Ping監視ツール",
                "ts": int(datetime.now().timestamp())
            }]
        }

        # Slack通知送信
        try:
            headers = {"Content-Type": "application/json"}
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    print(f"[SLACK] 通知送信成功: {host} -> {status_text}")
                else:
                    print(f"[SLACK] 通知送信失敗: HTTP {response.status}")
        except urllib.error.URLError as e:
            print(f"[SLACK] 通知送信エラー: {e}")
        except Exception as e:
            print(f"[SLACK] 予期しないエラー: {e}")

    def load_hosts(self):
        """ホストリストファイルを読み込む"""
        try:
            with open(self.hosts_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # コメント行と空行をスキップ
                    if line and not line.startswith('#'):
                        self.hosts.append(line)
            print(f"[INFO] {len(self.hosts)}台のホストを読み込みました")
            return True
        except FileNotFoundError:
            print(f"[ERROR] ファイル '{self.hosts_file}' が見つかりません")
            return False
        except Exception as e:
            print(f"[ERROR] ファイル読み込みエラー: {e}")
            return False

    def ping(self, host):
        """
        指定ホストにPingを実行

        Args:
            host: 対象ホスト（IPアドレスまたはホスト名）

        Returns:
            bool: 疎通成功時True、失敗時False
        """
        # OS判定（Windows/Unix系でpingコマンドのオプションが異なる）
        param = '-n' if platform.system().lower() == 'windows' else '-c'
        timeout_param = '-w' if platform.system().lower() == 'windows' else '-W'

        try:
            # Ping実行（1回、タイムアウト2秒）
            result = subprocess.run(
                ['ping', param, '1', timeout_param, '2', host],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False

    def monitor_host(self, host):
        """
        特定ホストを継続監視（スレッドで実行）

        Args:
            host: 監視対象ホスト
        """
        while self.running:
            is_alive = self.ping(host)
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            with self.lock:
                # 前回の状態を取得
                previous_alive = self.previous_status.get(host, {}).get('alive', None)

                # 現在の状態を更新
                self.status[host] = {
                    'alive': is_alive,
                    'timestamp': timestamp
                }

                # 連続NG回数の管理
                down_threshold = self.config.get("slack", {}).get("down_threshold", 10)

                if not is_alive:
                    # NGの場合、カウントを増やす
                    self.consecutive_failures[host] = self.consecutive_failures.get(host, 0) + 1
                    failure_count = self.consecutive_failures[host]

                    # 連続NGがしきい値に達した場合のみ通知（初回のみ）
                    if failure_count == down_threshold:
                        # 初回ダウン検知のため、メンション付きで通知
                        include_mention = not self.mention_sent.get(host, False)
                        self.send_slack_notification(host, is_alive, timestamp, include_mention, failure_count)
                        self.mention_sent[host] = True  # メンション送信済みフラグを立てる
                else:
                    # OKの場合
                    if previous_alive is not None and not previous_alive:
                        # NG→OK（復旧）の場合
                        failure_count = self.consecutive_failures.get(host, 0)
                        # 連続NGがしきい値以上だった場合のみ復旧通知
                        if failure_count >= down_threshold:
                            self.send_slack_notification(host, is_alive, timestamp, False, 0)

                    # カウントとフラグをリセット
                    self.consecutive_failures[host] = 0
                    self.mention_sent[host] = False

                # 前回の状態を更新
                self.previous_status[host] = {
                    'alive': is_alive,
                    'timestamp': timestamp
                }

            time.sleep(self.interval)

    def display_status(self):
        """ステータスを表示（定期的に更新）"""
        while self.running:
            time.sleep(2)  # 2秒ごとに表示更新

            with self.lock:
                if not self.status:
                    continue

                # 画面クリア（簡易版）
                print("\n" * 50)
                print("=" * 80)
                print(f"{'社内NW端末 Ping監視ツール':^80}")
                print("=" * 80)
                slack_status = "有効" if self.config.get("slack", {}).get("enabled", False) else "無効"
                print(f"監視対象: {len(self.hosts)}台 | 監視間隔: {self.interval}秒 | Slack通知: {slack_status}")
                print("-" * 80)
                print(f"{'ホスト':<20} {'状態':<15} {'連続NG':<10} {'最終確認時刻':<25}")
                print("-" * 80)

                # ステータス順にソート（NG→OK）
                sorted_hosts = sorted(
                    self.status.items(),
                    key=lambda x: (x[1]['alive'], x[0])
                )

                for host, info in sorted_hosts:
                    status_mark = "✓ OK" if info['alive'] else "✗ NG"
                    failure_count = self.consecutive_failures.get(host, 0)
                    failure_display = f"{failure_count}回" if failure_count > 0 else "-"
                    print(f"{host:<20} {status_mark:<15} {failure_display:<10} {info['timestamp']:<25}")

                print("-" * 80)
                print("[Ctrl+C で終了]")

    def start(self):
        """監視を開始"""
        if not self.load_hosts():
            return

        if not self.hosts:
            print("[ERROR] 監視対象ホストがありません")
            return

        self.running = True
        threads = []

        # 各ホストの監視スレッドを起動
        for host in self.hosts:
            thread = threading.Thread(target=self.monitor_host, args=(host,), daemon=True)
            thread.start()
            threads.append(thread)

        # 表示スレッドを起動
        display_thread = threading.Thread(target=self.display_status, daemon=True)
        display_thread.start()

        print("[INFO] 監視を開始しました（Ctrl+Cで終了）")

        try:
            # メインスレッドは待機
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n[INFO] 監視を終了します...")
            self.running = False
            time.sleep(1)


def main():
    """メイン処理"""
    # デフォルト設定で監視開始
    monitor = PingMonitor(hosts_file="hosts.txt", interval=5)
    monitor.start()


if __name__ == "__main__":
    main()
