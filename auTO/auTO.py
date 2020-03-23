import aiohttp
import asyncio
import discord
from discord.ext import commands
import functools
import logging
import re
from typing import Optional
import yaml

from . import challonge
from . config import config

logging.basicConfig(level=logging.INFO)


def istrcmp(a: str, b: str) -> bool:
    return a.lower() == b.lower()


async def send_list(ctx, the_list):
    """Send multi-line messages."""
    await ctx.send('\n'.join(the_list))


async def get_dms(owner):
    return (owner.dm_channel if owner.dm_channel else await owner.create_dm())


class Tournament(object):
    """Tournaments are unique to a guild + channel."""
    def __init__(self, ctx, tournament_id, api_key, session):
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.owner = ctx.author
        self.open_matches = []
        self.called_matches = set()
        self.recently_called = set()
        self.gar = challonge.Challonge(api_key, tournament_id, session)

    async def get_open_matches(self):
        matches = await self.gar.get_matches()
        self.open_matches = [m for m in matches if m['state'] == 'open']

    async def mark_match_underway(self, user1, user2):
        match_id = None

        for user in [user1, user2]:
            match = self.find_match(user.display_name)
            if match is None:
                return
            elif match_id is None:
                match_id = match['id']
            elif match_id != match['id']:
                return

        await self.gar.mark_underway(match_id)

    def find_match(self, username):
        for match in self.open_matches:
            if username.lower() in map(lambda s: s.lower(), [match['player1'],
                                       match['player2']]):
                return match
        else:
            return None

    def mention_user(self, username: str) -> str:
        """Gets the user mention string. If the user isn't found, just return
        the username."""
        for member in self.guild.members:
            if istrcmp(member.display_name, username):
                return member.mention
        return username

    def has_user(self, username: str) -> bool:
        """Finds if username is on the server."""
        return any(istrcmp(m.display_name, username)
                   for m in self.guild.members)

    async def report_match(self, match, winner_id, reporter, scores_csv):
        await self.add_to_recently_called(match, reporter)
        await self.gar.report_match(
                match['id'], winner_id, scores_csv)
        self.called_matches.remove(match['id'])

    async def add_to_recently_called(self, match, reporter):
        """Prevent both players from reporting at the same time."""
        if istrcmp(match['player1'], reporter):
            other = match['player2']
        else:
            other = match['player1']
        self.recently_called.add(other)
        await asyncio.sleep(5)
        self.recently_called.remove(other)

    async def missing_tags(self, owner) -> bool:
        """Check the participants list for players not on the server."""
        dms = await get_dms(owner)
        missing = [player for player in self.gar.get_players()
                   if not self.has_user(player)]
        if not missing:
            return False
        message = ['Missing Discord accounts for the following players:']
        for p in missing:
            message.append('- {}'.format(p))
        await send_list(dms, message)
        return True

    @classmethod
    def key(cls, ctx):
        return ctx.guild, ctx.channel


class TOCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None
        self.tournament_map = {}
        self.bot.loop.create_task(self.create_session())

    async def create_session(self):
        await self.bot.wait_until_ready()
        self.session = aiohttp.ClientSession(raise_for_status=True)

    def get_tourney(self, ctx=None, guild=None, channel=None):
        if ctx is None:
            key = (guild, channel)
        else:
            key = Tournament.key(ctx)
        return self.tournament_map.get(key)

    def tourney_start(self, ctx, tournament_id, api_key):
        tourney = Tournament(ctx, tournament_id, api_key, self.session)
        self.tournament_map[Tournament.key(ctx)] = tourney
        return tourney

    def tourney_stop(self, ctx):
        self.tournament_map.pop(Tournament.key(ctx))

    @commands.group(case_insensitive=True)
    async def auTO(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.send('Use `!auto help` for options')

    @auTO.command()
    async def help(self, ctx):
        help_list = [
            '- `start [URL]` - start TOing',
            '- `stop` - stop TOing',
            '- `update_tags` - get the latest Challonge tags',
            '- `report 0-2` - report a match',
            '- `matches` - print the active matches',
            '- `status` - show how far along the tournament is',
        ]
        await send_list(ctx, help_list)

    def has_tourney(func):
        """Decorator that returns if no tourney is set."""
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            ctx = args[0]
            tourney = self.get_tourney(ctx)
            if tourney is None:
                await ctx.send('No tournament running')
                return
            kwargs['tourney'] = tourney
            return await func(self, *args, **kwargs)
        return wrapper

    def is_to(func):
        """Decorator that ensures caller is owner, TO, or admin."""
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            ctx = args[0]
            user = ctx.author
            tourney = kwargs['tourney']
            if not (user == tourney.owner or
                    tourney.channel.permissions_for(user).administrator or
                    any(role.name == 'TO' for role in user.roles)):
                await ctx.send('Only a TO can run this command.')
                return
            return await func(self, *args, **kwargs)
        return wrapper

    @auTO.command()
    @has_tourney
    @is_to
    async def update_tags(self, ctx, *, tourney=None):
        await tourney.gar.update_data('participants')

    @auTO.command()
    @has_tourney
    async def status(self, ctx, *, tourney=None):
        await ctx.trigger_typing()
        await ctx.send('Tournament is {}% completed.'
                       .format(await tourney.gar.progress_meter()))

    def is_dm_response(self, owner):
        return lambda m: m.channel == owner.dm_channel and m.author == owner

    async def ask_for_challonge_key(self,
                                    owner: discord.Member) -> Optional[str]:
        """DM the TO for their Challonge key."""
        dms = await get_dms(owner)
        await dms.send("Hey there! To run this tournament for you, I'll need "
                       "your Challonge API key "
                       "(https://challonge.com/settings/developer). "
                       "The key is only used to run the bracket and is "
                       "deleted after the tournament finishes.")
        await dms.send("If that's ok with you, respond to this message with "
                       "your Challonge API key, otherwise, with 'NO'.")

        while True:
            msg = await self.bot.wait_for(
                    'message', check=self.is_dm_response(owner))

            content = msg.content.strip()
            if istrcmp(content, 'no'):
                await dms.send('👍')
                return None
            elif re.match(r'[a-z0-9]+$', content, re.I):
                return content
            else:
                await dms.send('Invalid API key, try again.')

    async def confirm(self, ctx, question) -> bool:
        """DM the user a yes/no question."""
        dms = await get_dms(ctx.author)
        await dms.send('{} [Y/n]'.format(question))
        msg = await self.bot.wait_for(
                'message', check=self.is_dm_response(ctx.author))

        return msg.content.strip().lower() in ['y', 'yes']

    @auTO.command(brief='Challonge URL of tournament')
    async def start(self, ctx, url: str):
        """Sets tournament URL and start calling matches."""
        if self.get_tourney(ctx) is not None:
            await ctx.send('A tournament is already in progress')
            return

        try:
            tournament_id = challonge.extract_id(url)
        except ValueError as e:
            await ctx.send(e)
            return

        # Useful for debugging.
        api_key = config.get('CHALLONGE_KEY')
        if api_key is None:
            api_key = await self.ask_for_challonge_key(ctx.author)
            if api_key is None:
                return

        tourney = self.tourney_start(ctx, tournament_id, api_key)
        try:
            await tourney.gar.get_raw()
        except aiohttp.client_exceptions.ClientResponseError as e:
            if e.code == 401:
                await ctx.author.dm_channel.send('Invalid API Key')
                self.tourney_stop(ctx)
                return
            else:
                raise e

        if tourney.gar.get_state() == 'pending':
            await ctx.send("Tournament hasn't been started yet.")
            self.tourney_stop(ctx)
            return
        elif tourney.gar.get_state() == 'ended':
            await ctx.send("Tournament has already finished.")
            self.tourney_stop(ctx)
            return

        has_missing = await tourney.missing_tags(ctx.author)
        if has_missing:
            confirm = await self.confirm(ctx, 'Continue anyway?')
            if confirm:
                await self.update_tags(ctx)
            else:
                self.tourney_stop(ctx)
                return

        activity = discord.Activity(name='Dolphin',
                                    type=discord.ActivityType.watching)
        await self.bot.change_presence(activity=activity)

        await ctx.trigger_typing()
        logging.info('Starting tournament {} on {}'.format(
            tourney.gar.get_name(), tourney.guild.name))
        start_msg = await ctx.send('Starting {}! {}'.format(
            tourney.gar.get_name(), tourney.gar.get_url()))
        await start_msg.pin()
        await self.matches(ctx)

    @auTO.command()
    @has_tourney
    @is_to
    async def stop(self, ctx, *, tourney=None):
        self.tourney_stop(ctx)
        await self.bot.change_presence()
        await ctx.send('Goodbye 😞')

    async def end_tournament(self, ctx, tourney):
        confirm = await self.confirm(ctx, '{} is completed. Finalize?'
                                     .format(tourney.gar.get_name()))
        if not confirm:
            return

        try:
            await tourney.gar.finalize()
        except aiohttp.client_exceptions.ClientResponseError as e:
            if e == 422:
                # Tournament's already finalized.
                pass
            else:
                raise e
        await self.results(ctx)

    @auTO.command()
    @has_tourney
    async def results(self, ctx, *, tourney=None):
        top8 = await tourney.gar.get_top8()
        if top8 is None:
            return

        winner = tourney.mention_user(top8[0][1][0])
        message = [
            'Congrats to the winner of {}: **{}**!!'.format(
                tourney.gar.get_name(), winner),
            'We had {} entrants!\n'.format(len(tourney.gar.get_players())),
        ]

        for i, players in top8:
            players = ' / '.join(map(tourney.mention_user, players))
            message.append('{}. {}'.format(i, players))

        await send_list(ctx, message)
        self.tourney_stop(ctx)
        await self.bot.change_presence()

    @auTO.command()
    @has_tourney
    async def matches(self, ctx, *, tourney=None):
        """Checks for match updates and prints matches to the channel."""
        await ctx.trigger_typing()
        await tourney.get_open_matches()

        if not tourney.open_matches:
            await self.end_tournament(ctx, tourney)
            return

        announcement = []
        for m in tourney.open_matches:
            player1 = m['player1']
            player2 = m['player2']

            # We want to only ping players the first time their match is
            # called.
            if m['id'] not in tourney.called_matches:
                player1 = tourney.mention_user(player1)
                player2 = tourney.mention_user(player2)
                tourney.called_matches.add(m['id'])

            match = '**{}**: {} vs {}'.format(m['round'], player1, player2)
            if m['underway']:
                match += ' (Playing)'
            announcement.append(match)

        await send_list(ctx, announcement)

    @auTO.command(brief='Report match results')
    @has_tourney
    async def report(self, ctx, scores_csv: str, *, tourney=None):
        if not re.match(r'\d-\d', scores_csv):
            await ctx.send('Invalid report. Should be `!auto report 0-2`')
            return

        scores = [int(n) for n in scores_csv.split('-')]

        if scores[0] > scores[1]:
            player1_win = True
        elif scores[0] < scores[1]:
            player1_win = False
        else:
            await ctx.send('No ties allowed.')
            return

        match_id = None
        winner_id = None
        username = ctx.author.display_name

        if username.lower() in tourney.recently_called:
            await ctx.send('Ignoring potentially duplicate report. Try again '
                           'in a couple seconds if this is incorrect.')
            return

        match = tourney.find_match(username)
        if match is None:
            await ctx.send('{} not found in current matches'.format(username))
            return

        match_id = match['id']
        if istrcmp(username, match['player2']):
            # Scores are reported with player1's score first.
            scores_csv = scores_csv[::-1]
            player1_win = not player1_win

        if player1_win:
            winner_id = match['player1_id']
        else:
            winner_id = match['player2_id']

        await ctx.trigger_typing()
        await tourney.report_match(match, winner_id, username, scores_csv)
        await self.matches(ctx)

    @commands.Cog.listener()
    async def on_command_error(self, ctx, err):
        if isinstance(err, commands.CommandNotFound):
            # These are useless and clutter the log.
            return
        if not isinstance(err, commands.MissingRequiredArgument):
            raise err

        if ctx.invoked_subcommand.name == 'start':
            await ctx.send('Tournament URL is required')
        else:
            await ctx.send(err)

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info('auTO has connected to Discord')

    @commands.Cog.listener()
    async def on_message(self, message):
        tourney = self.get_tourney(guild=message.guild,
                                   channel=message.channel)
        if tourney is None:
            return

        if message.content == '!bracket':
            await message.channel.send(tourney.gar.get_url())
        # If someone posts a netplay code for their opponent, mark their
        # match as underway.
        elif (len(message.mentions) == 1 and
              re.search(r'\b[a-f0-9]{8}\b', message.content)):
            await tourney.mark_match_underway(
                    message.mentions[0], message.author)


def main():
    bot = commands.Bot(command_prefix='!', description='Talk to the TO',
                       case_insensitive=True)
    bot.add_cog(TOCommands(bot))
    bot.run(config.get('DISCORD_TOKEN'))


if __name__ == '__main__':
    main()
