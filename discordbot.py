# MBTI診断Discord Bot

import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncpg
from datetime import datetime
import asyncio
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import io
import json
from discord import Interaction, app_commands

# --- 環境変数ロード ---
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USER_IDS = [int(id) for id in os.getenv("ADMIN_USER_IDS", "").split(',') if id]

# --- Discord Intents ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- ギルド・チャンネルIDはすべてテスト用架空値 ---
CHANNEL_MAP = {
    1000000000000000000: [1100000000000000001, 1100000000000000002],  # example
    2000000000000000000: [2100000000000000001, 2100000000000000002],
}
USER_CACHE = {}

# --- オートコンプリート用関数（管理コマンド向け） ---
async def user_autocomplete(interaction: Interaction, current: str):
    choices = []
    if not interaction.guild:
        return []
    members = interaction.guild.members
    USER_CACHE.clear()
    for member in members:
        if member.bot:
            continue
        display_name_with_id = f"{member.display_name} ({member.id})"
        USER_CACHE[display_name_with_id] = member.id
        if current.lower() in display_name_with_id.lower():
            choices.append(app_commands.Choice(name=display_name_with_id, value=display_name_with_id))
        if len(choices) >= 25:
            break
    return choices

# --- MBTI設問リスト（サンプル：最初の4問＋A/T系2問のみ記載） ---
questions = [
    {"text": "初対面の人と話すのはエネルギーを使うと感じる。", "dimension": "EI", "weights": ["E", "E", "I", "I"]},
    {"text": "1人で静かに過ごす時間が好きだ。", "dimension": "EI", "weights": ["E", "E", "I", "I"]},
    {"text": "話すより聞くほうが自然だと思う。", "dimension": "EI", "weights": ["E", "E", "I", "I"]},
    {"text": "話題を自分から振るのはあまり得意ではない。", "dimension": "EI", "weights": ["E", "E", "I", "I"]},
    # ...（中略：本番は24問＋AT系4問）
    {"text": "自分の選択にはたいてい自信がある", "dimension": "AT", "weights": [0.0, 0.3, 0.6, 0.9]},
    {"text": "失敗しても引きずらず、次に切り替えられる", "dimension": "AT", "weights": [0.0, 0.3, 0.6, 0.9]},
]

# --- DB初期化・テーブル作成 ---
async def init_db():
    bot.db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with bot.db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS mbti_results (
            user_id BIGINT PRIMARY KEY, username TEXT NOT NULL, result TEXT NOT NULL,
            subtype TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS mbti_history (
            id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, username TEXT NOT NULL,
            result TEXT NOT NULL, subtype TEXT, scores JSONB, at_scores JSONB,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

# --- 質問ボタンView ---
class QuestionView(discord.ui.View):
    def __init__(self, user_id, index=0, scores=None, at_scores=None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.index = index
        self.main_scores = scores or {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        self.main_total_score = sum(self.main_scores.values()) if scores is not None else 0.0
        self.at_scores = at_scores or []
        self.at_total_score = sum(self.at_scores)
        for i, label in enumerate(["A","B","C","D"]):
            self.add_item(self._make_button(label, i))

    def _make_button(self, label, idx):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("これはあなただけの診断です。", ephemeral=True)
            current_question = questions[self.index]
            if self.index < 24:
                trait = current_question["weights"][idx]
                score_value = [0.3, 0.6, 0.9, 1.2][idx]
                self.main_scores[trait] += score_value
                self.main_total_score += score_value
            else:
                at_score_value = current_question["weights"][idx]
                self.at_scores.append(at_score_value)
                self.at_total_score += at_score_value

            next_index = self.index + 1

            if next_index < len(questions):
                await interaction.response.edit_message(
                    content=format_question(next_index),
                    view=QuestionView(self.user_id, next_index, self.main_scores, self.at_scores)
                )
            else:
                mbti = get_mbti_type(self.main_scores)
                main_average = self.main_total_score / 24.0
                at_average = self.at_total_score / 4.0
                subtype = get_mbti_subtype(main_average, at_average)
                full = f"{mbti}-{subtype}"
                embed = discord.Embed(
                    title=f"{interaction.user.display_name} さんのMBTIタイプは「{full}」！",
                    description=type_descriptions.get(mbti, "説明なし"),
                    color=discord.Color.green()
                )
                async with interaction.client.db_pool.acquire() as conn:
                    async with conn.transaction():
                        row = await conn.fetchrow("SELECT result, subtype FROM mbti_results WHERE user_id=$1", interaction.user.id)
                        if row:
                            embed.add_field(name="前回の診断結果", value=f"{row['result']}-{row['subtype']}", inline=False)
                        top_matches = get_top_compatibility_types(mbti)
                        if top_matches:
                            embed.add_field(name="相性の良いタイプTOP3", value="、".join(top_matches), inline=False)
                        subtype_reason = "自己主張型" if subtype == "A" else "慎重型"
                        supplement_text = (
                            f"📌 補足：\n"
                            f"・本編平均スコア（最大1.2）：{main_average:.2f}\n"
                            f"・A/T設問平均スコア（最大0.9）：{at_average:.2f}\n"
                            f"→ これらのスコアに基づき「{subtype}（{subtype_reason}）」と判定されました。"
                        )
                        embed.add_field(name="\u200b", value=supplement_text, inline=False)
                        await conn.execute("""
                            INSERT INTO mbti_results(user_id,username,result,subtype,timestamp)
                            VALUES($1,$2,$3,$4,$5) ON CONFLICT(user_id) DO UPDATE
                            SET result=$3,subtype=$4,username=$2,timestamp=$5
                        """, interaction.user.id, interaction.user.display_name, mbti, subtype, datetime.utcnow())
                        count = await conn.fetchval("SELECT COUNT(*) FROM mbti_history WHERE user_id = $1", interaction.user.id)
                        if count >= 5:
                            await conn.execute("""
                                DELETE FROM mbti_history WHERE id = (
                                    SELECT id FROM mbti_history WHERE user_id = $1 ORDER BY timestamp ASC LIMIT 1
                                )
                            """, interaction.user.id)
                        scores_dict = {k: round(v, 2) for k, v in self.main_scores.items()}
                        await conn.execute("""
                            INSERT INTO mbti_history (user_id, username, result, subtype, scores, at_scores, timestamp)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """, interaction.user.id, interaction.user.display_name, mbti, subtype, json.dumps(scores_dict), json.dumps(self.at_scores), datetime.utcnow())
                await interaction.response.edit_message(content="診断完了！DM送信します", view=None)
                await asyncio.sleep(3)
                try:
                    await interaction.user.send(embed=embed)
                except discord.Forbidden:
                    await interaction.followup.send("診断完了！DM送信できませんでした。", ephemeral=True)

        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id=f"q_{self.index}_{idx}")
        button.callback = cb
        return button

# --- 診断UIメインメニューView ---
class StartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="診断開始", style=discord.ButtonStyle.success, custom_id="mbti_start", row=0))
        self.add_item(discord.ui.Button(label="前回記録", style=discord.ButtonStyle.secondary, custom_id="mbti_prev", row=0))
        self.add_item(discord.ui.Button(label="履歴を見る", style=discord.ButtonStyle.primary, custom_id="mbti_history", row=1))
        self.add_item(discord.ui.Button(label="傾向を見る", style=discord.ButtonStyle.primary, custom_id="mbti_trend", row=1))

def get_mbti_type(scores: dict) -> str:
    return (("E" if scores["E"]>=scores["I"] else "I") + ("N" if scores["N"]>=scores["S"] else "S") +
            ("T" if scores["T"]>=scores["F"] else "F") + ("J" if scores["J"]>=scores["P"] else "P"))

def get_mbti_subtype(main_average: float, at_average: float) -> str:
    return "A" if main_average >= 0.725 or at_average >= 0.70 else "T"

def format_question(index):
    q = questions[index]
    intro = ""
    if index == 0:
        intro = ("📝 各質問には A～D の4つの選択肢があります。\n\n"
                 "A：まったくそう思わない\nB：あまりそう思わない\nC：そう思う\nD：強くそう思う\n\n")
    if index == 24:
        intro += "続いて、サブタイプ（自己主張型 / 慎重型）に関する4つの質問です。\n\n"
    return f"{intro}**Q{index+1}. {q['text']}**\nA: まったくそう思わない｜B: あまりそう思わない｜C: そう思う｜D: 強くそう思う"

# --- タイプ説明・相性（サンプル） ---
type_descriptions = {
    "INTJ": "戦略的な完璧主義者。目標達成のために未来を見据えて動く.",
    # ...本番では全16タイプ記載...
}
type_compatibility = {
    "INTJ": ["ENFP", "INFP", "ENTP"],
    # ...本番では全タイプ記載...
}
def get_top_compatibility_types(mbti_type: str) -> list:
    return type_compatibility.get(mbti_type[:4], [])[:3]

@bot.event
async def on_ready():
    await init_db()
    try:
        await bot.tree.sync()
        print("Slash commands synced successfully.")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")
    bot.add_view(StartView())
    print(f"{bot.user} is ready!")

async def handle_mbti_start(interaction: Interaction):
    await interaction.response.send_message(
        content=format_question(0),
        view=QuestionView(interaction.user.id, index=0, scores=None, at_scores=None),
        ephemeral=True
    )

async def handle_mbti_prev(interaction: Interaction):
    async with interaction.client.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT result, subtype, timestamp FROM mbti_results WHERE user_id=$1", interaction.user.id)
    if row:
        embed = discord.Embed(title="📜 前回のMBTI診断記録", color=discord.Color.blurple())
        embed.add_field(name="MBTIタイプ", value=f"{row['result']}-{row['subtype']}", inline=False)
        embed.set_footer(text=f"診断日: {row['timestamp'].strftime('%Y-%m-%d %H:%M')}")
        try:
            await interaction.user.send(embed=embed)
            await interaction.response.send_message("前回の診断結果をDM送信しました。", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("DM送信できませんでした。", ephemeral=True)
    else:
        await interaction.response.send_message("前回の診断はありません。", ephemeral=True)

async def handle_mbti_history(interaction: Interaction):
    await interaction.response.send_message("履歴を取得中です。", ephemeral=True)
    await asyncio.sleep(2)
    async with interaction.client.db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT result, subtype, timestamp FROM mbti_history WHERE user_id = $1 ORDER BY timestamp DESC LIMIT 5", interaction.user.id)
    if not rows:
        return await interaction.followup.send("履歴がありません。", ephemeral=True)
    embed = discord.Embed(title="🕓 MBTI診断の履歴", color=discord.Color.gold())
    for row in rows:
        embed.add_field(name=row['timestamp'].strftime('%Y-%m-%d %H:%M'), value=f"{row['result']}-{row['subtype']}", inline=False)
    try:
        await interaction.user.send(embed=embed)
        await interaction.followup.send("履歴をDMで送信しました。", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("DM送信できませんでした。", ephemeral=True)

async def handle_mbti_trend(interaction: Interaction):
    font_path = 'MPLUS1p-Regular.ttf'
    if os.path.exists(font_path):
        fm.fontManager.addfont(font_path)
        plt.rcParams['font.family'] = 'M PLUS 1p'
    await interaction.response.send_message(f"{interaction.user.display_name} さんの傾向を準備中です", ephemeral=True)
    await asyncio.sleep(3)
    async with interaction.client.db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT timestamp, scores, at_scores FROM mbti_history WHERE user_id = $1 ORDER BY timestamp ASC LIMIT 5", interaction.user.id)
    if not rows:
        return await interaction.followup.send("傾向データがありません。", ephemeral=True)
    # ...グラフ生成・送信（本番は実装済み）

@bot.event
async def on_interaction(interaction: Interaction):
    if interaction.type != discord.InteractionType.component or "custom_id" not in interaction.data:
        return
    custom_id = interaction.data["custom_id"]
    if custom_id == "mbti_start":
        await handle_mbti_start(interaction)
    elif custom_id == "mbti_prev":
        await handle_mbti_prev(interaction)
    elif custom_id == "mbti_history":
        await handle_mbti_history(interaction)
    elif custom_id == "mbti_trend":
        await handle_mbti_trend(interaction)

@bot.command()
async def mbti(ctx):
    await ctx.message.delete()
    embed = discord.Embed(
        title="MBTI診断",
        description="ボタンで診断スタート！自分の性格をチェックしよう。",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=StartView())

@bot.tree.command(name="delete", description="指定ユーザーのMBTI診断履歴を削除します（管理者用）")
@app_commands.describe(user="履歴を削除したいユーザーを選択", count="削除件数（1～5）")
@app_commands.autocomplete(user=user_autocomplete)
async def delete_command(interaction: Interaction, user: str, count: int):
    if interaction.user.id not in ADMIN_USER_IDS:
        return await interaction.response.send_message("このコマンドは管理者専用です。", ephemeral=True)
    if not 1 <= count <= 5:
        return await interaction.response.send_message("削除件数は1〜5で指定してください。", ephemeral=True)
    if user not in USER_CACHE:
        return await interaction.response.send_message("ユーザーが見つかりません。", ephemeral=True)
    user_id = USER_CACHE[user]
    try:
        async with bot.db_pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM mbti_history WHERE id IN (SELECT id FROM mbti_history WHERE user_id = $1 ORDER BY timestamp DESC LIMIT $2)", user_id, count)
        deleted_count = int(status.split()[-1]) if "DELETE" in status else 0
        await interaction.response.send_message(f"ユーザー `{user}` の診断履歴を {deleted_count}件 削除しました。")
    except Exception as e:
        print(f"Error deleting history for user {user_id} (selected: {user}): {e}")
        await interaction.response.send_message("エラーが発生しました。", ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)
