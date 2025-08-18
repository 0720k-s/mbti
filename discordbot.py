import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncpg
from datetime import datetime
import asyncio
import json

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USER_IDS = [int(i) for i in os.getenv("ADMIN_USER_IDS", "").split(',') if i]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

questions = [
    {"text": "初対面の人と話すのはエネルギーを使うと感じる。", "dimension": "EI", "weights": ["E", "E", "I", "I"]},
    {"text": "1人で静かに過ごす時間が好きだ。", "dimension": "EI", "weights": ["E", "E", "I", "I"]},
    {"text": "話すより聞くほうが自然だと思う。", "dimension": "EI", "weights": ["E", "E", "I", "I"]},
    {"text": "話題を自分から振るのはあまり得意ではない。", "dimension": "EI", "weights": ["E", "E", "I", "I"]},
    {"text": "自分の選択にはたいてい自信がある", "dimension": "AT", "weights": [0.0, 0.3, 0.6, 0.9]},
    {"text": "失敗しても引きずらず、次に切り替えられる", "dimension": "AT", "weights": [0.0, 0.3, 0.6, 0.9]},
]

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

class QuestionView(discord.ui.View):
    def __init__(self, user_id, index=0, scores=None, at_scores=None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.index = index
        self.main_scores = scores or {"E":0,"I":0,"S":0,"N":0,"T":0,"F":0,"J":0,"P":0}
        self.main_total_score = sum(self.main_scores.values()) if scores else 0.0
        self.at_scores = at_scores or []
        self.at_total_score = sum(self.at_scores)
        for i, label in enumerate(["A","B","C","D"]):
            self.add_item(self._make_button(label, i))

    def _make_button(self, label, idx):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("自分用のボタンです。", ephemeral=True)
            q = questions[self.index]
            if self.index < 24:
                t = q["weights"][idx]
                v = [0.3, 0.6, 0.9, 1.2][idx]
                self.main_scores[t] += v
                self.main_total_score += v
            else:
                v = q["weights"][idx]
                self.at_scores.append(v)
                self.at_total_score += v

            next_index = self.index + 1
            if next_index < len(questions):
                await interaction.response.edit_message(
                    content=format_question(next_index),
                    view=QuestionView(self.user_id, next_index, self.main_scores, self.at_scores)
                )
            else:
                mbti = get_mbti_type(self.main_scores)
                main_avg = self.main_total_score / 24.0 if self.main_total_score else 0
                at_avg = self.at_total_score / 4.0 if self.at_scores else 0
                subtype = "A" if main_avg >= 0.725 or at_avg >= 0.70 else "T"
                result = f"{mbti}-{subtype}"
                embed = discord.Embed(title=f"{interaction.user.display_name} さんのMBTIタイプ", description=result, color=0x44bd32)
                async with interaction.client.db_pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO mbti_results(user_id,username,result,subtype,timestamp)
                        VALUES($1,$2,$3,$4,$5)
                        ON CONFLICT(user_id) DO UPDATE SET result=$3,subtype=$4,username=$2,timestamp=$5
                    """, interaction.user.id, interaction.user.display_name, mbti, subtype, datetime.utcnow())
                    await conn.execute("""
                        INSERT INTO mbti_history (user_id, username, result, subtype, scores, at_scores, timestamp)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """, interaction.user.id, interaction.user.display_name, mbti, subtype, json.dumps(self.main_scores), json.dumps(self.at_scores), datetime.utcnow())
                await interaction.response.edit_message(content="診断おわり！", view=None)
                await asyncio.sleep(2)
                try:
                    await interaction.user.send(embed=embed)
                except:
                    pass
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id=f"q_{self.index}_{idx}")
        btn.callback = cb
        return btn

def get_mbti_type(scores: dict) -> str:
    return (("E" if scores["E"]>=scores["I"] else "I") + ("N" if scores["N"]>=scores["S"] else "S") +
            ("T" if scores["T"]>=scores["F"] else "F") + ("J" if scores["J"]>=scores["P"] else "P"))

def format_question(index):
    q = questions[index]
    intro = ""
    if index == 0:
        intro = ("A: まったくそう思わない / B: あまり思わない / C: そう思う / D: 強くそう思う\n\n")
    return f"{intro}Q{index+1}. {q['text']}"

class StartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="診断開始", style=discord.ButtonStyle.success, custom_id="mbti_start"))

@bot.event
async def on_ready():
    await init_db()
    try:
        await bot.tree.sync()
    except Exception as e:
        print(e)
    bot.add_view(StartView())
    print(f"{bot.user} is ready!")

@bot.event
async def on_interaction(interaction):
    if interaction.type != discord.InteractionType.component or "custom_id" not in interaction.data:
        return
    cid = interaction.data["custom_id"]
    if cid == "mbti_start":
        await interaction.response.send_message(
            content=format_question(0),
            view=QuestionView(interaction.user.id, index=0, scores=None, at_scores=None),
            ephemeral=True
        )

@bot.command()
async def mbti(ctx):
    await ctx.message.delete()
    embed = discord.Embed(
        title="MBTI診断",
        description="診断開始を押してね",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=StartView())

if __name__ == "__main__":
    bot.run(TOKEN)
