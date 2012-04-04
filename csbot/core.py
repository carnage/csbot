from functools import wraps
import shlex
import ConfigParser

from twisted.words.protocols import irc
from twisted.internet import reactor, protocol
from twisted.python import log


def hook(f):
    """Create a plugin hook.

    Used as a method decorator this will cause the hook of the same name to be
    fired after the method.  Used to create a new method, *f* names the hook
    that will be fired by the method.
    """
    if callable(f):
        @wraps(f)
        def newf(self, *args, **kwargs):
            f(self, *args, **kwargs)
            self.fire_hook(f.__name__, *args, **kwargs)
    else:
        def newf(self, *args, **kwargs):
            self.fire_hook(f, *args, **kwargs)
        newf.__doc__ = "Generated hook trigger for ``{}``".format(f)
    return newf


def command(name, raw=False):
    """Mark a plugin method as a bot command.

    Using this decorator is shorthand for registering the method as a command
    in the plugin setup method.  Commands created with the decorator will be
    registered in the plugin's constructor, i.e. before any setup methods are
    run.

    .. seealso:: :meth:`Bot.register_command`
    """
    def decorate(f):
        f.command = {'name': name}
        return f
    return decorate


def nick(user):
    """Get nick from user string.

    >>> nick('csyorkbot!~csbot@example.com')
    'csyorkbot'
    """
    return user.split('!', 1)[0]


def username(user):
    """Get username from user string.

    >>> username('csyorkbot!~csbot@example.com')
    'csbot'
    """
    return user.rsplit('@', 1)[0].rsplit('~', 1)[1]


def host(user):
    """Get hostname from user string.

    >>> host('csyorkbot!~csbot@example.com')
    'example.com'
    """
    return user.rsplit('@', 1)[1]


def is_channel(channel):
    """Check if *channel* is a channel or private chat.

    >>> is_channel('#cs-york')
    True
    >>> is_channel('csyorkbot')
    False
    """
    return channel.startswith('#')


def plugin_name(obj):
    """Get the name for a plugin object.

    A plugin's name is its fully qualified module path, excluding the leading
    component (which will always be ``csbot_plugins``).

    >>> from csbot.plugins.example import EmptyPlugin
    >>> plugin_name(EmptyPlugin(None))
    'example.EmptyPlugin'
    """
    return (obj.__class__.__module__.split('.', 2)[2] +
            '.' + obj.__class__.__name__)


class Bot(irc.IRCClient):

    def __init__(self, config, plugins):
        # Load the configuration file, with default values
        # for the global settings if missing.
        self.cfgfile = config

        self.config = ConfigParser.SafeConfigParser(defaults={
            "nickname": "csyorkbot",
            "username": "csyorkbot",
            "realname": "cs-york bot",
            "sourceURL": "http://github.com/csyork/csbot/",
            "lineRate": "1"},
            allow_no_value=True)

        self.config.read(self.cfgfile)

        self.nickname = self.config.get("DEFAULT", "nickname")
        self.username = self.config.get("DEFAULT", "username")
        self.realname = self.config.get("DEFAULT", "realname")
        self.sourceURL = self.config.get("DEFAULT", "sourceURL")
        self.lineRate = self.config.getint("DEFAULT", "lineRate")

        self.commands = dict()
        self.plugins = dict()

        for P in plugins:
            p = P(self)
            name = plugin_name(p)
            if name in self.plugins:
                self.log_err('Duplicate plugin name: ' + name)
            else:
                self.plugins[name] = p
                self.log_msg('Loaded plugin: ' + name)

    def fire_hook(self, hook, *args, **kwargs):
        """Call *hook* on every plugin that has implemented it"""
        for plugin in self.plugins.itervalues():
            f = getattr(plugin, hook, None)
            if callable(f):
                f(*args, **kwargs)

    def register_command(self, command, f):
        """Register *f* as the callback for *command*.

        The callback will be called with a :class:`CommandEvent` object.

        Returns False if the command already exists, otherwise returns True.
        """
        if command in self.commands:
            self.log_err('Command {} already registered'.format(command))
            return False
        self.commands[command] = {'f': f}
        return True

    def fire_command(self, command):
        """Dispatch *command* to its callback."""
        if command.command not in self.commands:
            command.error('Command "{0.command}" not found'.format(command))
            return

        handler = self.commands[command.command]
        handler['f'](command)

    def reply(self, user, channel, msg):
        """Send a reply message to *channel*.

        Plugins should use this instead of :meth:`msg`.  It automatically
        handles replying to the user correctly in private chats and addressing
        the user by name in channels.
        """
        if channel == self.nickname:
            self.msg(nick(user), msg)
        else:
            self.msg(channel, nick(user) + ': ' + msg)

    def error(self, user, channel, msg, direct):
        """Respond with an error.

        Log *msg* as an error, and if *direct* is True also respond in the
        channel with the error message.  (This is to prevent the bot spamming
        the channel with errors for things it doesn't understand unless
        addressed explicitly.)
        """
        self.log_err(msg)
        if direct:
            self.reply(user, channel, "Error: " + msg)

    def log_msg(self, msg):
        """Convenience wrapper around ``twisted.python.log.msg`` for plugins"""
        log.msg(msg)

    def log_err(self, err):
        """Convenience wrapper around ``twisted.python.log.err`` for plugins"""
        log.err(err)

    def connectionMade(self):
        irc.IRCClient.connectionMade(self)
        print "[Connected]"
        for p in self.plugins.itervalues():
            p.setup()

    def connectionLost(self, reason):
        irc.IRCClient.connectionLost(self, reason)
        print "[Disconnected because {}]".format(reason)
        for p in self.plugins.itervalues():
            p.teardown()

    def signedOn(self):
        map(self.join, self.factory.channels)

    @hook
    def privmsg(self, user, channel, msg):
        """Handle commands in channel messages.
        """
        command = CommandEvent.create(self, user, channel, msg)
        if command is not None:
            self.fire_command(command)

    action = hook('action')


class Plugin(object):
    """Bot plugin base class.

    All plugins should subclass this class to be automatically detected and
    loaded.
    """
    def __init__(self, bot):
        self.bot = bot
        self.plugin = plugin_name(self)

        # Register decorated commands
        for k in dir(self):
            if not k.startswith('_'):
                f = getattr(self, k)
                if hasattr(f, 'command'):
                    self.bot.register_command(f.command['name'], f)

    def cfg(self, name):
        # Check plugin config
        if self.bot.config.has_section(self.plugin):
            if self.bot.config.has_option(self.plugin, name):
                return self.bot.config.get(self.plugin, name)

        # Check default config
        if self.bot.config.has_option("DEFAULT", name):
            return self.bot.config.get("DEFAULT", name)

        # Raise an exception
        raise KeyError("{} is not a valid option.".format(name))

    def setup(self):
        pass

    def teardown(self):
        pass


class CommandEvent(object):
    #: The :class:`Bot` this command was received by
    bot = None
    #: The command invoked (minus any trigger characters)
    command = None
    #: User string for the source of the command
    user = None
    #: Channel that the command was received on
    channel = None
    #: False if the command was triggered by the command prefix, True otherwise
    direct = False
    #: The rest of the line after the command name
    raw_data = None
    #: Cached argument list, see :attr:`data`
    data_ = None

    def __init__(self, bot, user, channel, command, direct, raw_data):
        self.bot = bot
        self.command = command
        self.user = user
        self.channel = channel
        self.direct = direct
        self.raw_data = raw_data
        self.data_ = None

    @staticmethod
    def create(bot, user, channel, msg):
        """Attempt to create an event from *msg*.

        Returns None if *msg* is not a command, otherwise returns a new
        :class:`CommandEvent`.
        """
        command = None
        direct = False

        if is_channel(channel):
            # In channel, must be triggered explicitly
            if msg.startswith(bot.factory.command_prefix):
                # Triggered by command prefix: "<prefix><cmd> <args>"
                command = msg[len(bot.factory.command_prefix):]
            elif msg.startswith(bot.nickname):
                # Addressing the bot by name: "<nick>, <cmd> <args>"
                msg = msg[len(bot.nickname):].lstrip()
                # Check that the bot was specifically addressed, rather than
                # a similar nick or just talking about the bot
                if len(msg) > 0 and msg[0] in ',:;.':
                    command = msg.lstrip(',:;.')
                    direct = True
        else:
            command = msg
            direct = True

        if command is None or command.strip() == '':
            return None

        command = command.split(None, 1)
        cmd = command[0]
        data = command[1] if len(command) == 2 else ''
        return CommandEvent(bot, user, channel, cmd, direct, data)

    @property
    def data(self):
        """Command data as an argument list.

        :attr:`raw_data` is processed using :py:func:`shlex.split` the first
        time this attribute is accessed.  If this fails, :meth:`error` is
        called and a :py:class:`ValueError` is raised.
        """
        if self.data_ is None:
            try:
                self.data_ = shlex.split(self.raw_data, posix=False)
            except ValueError as e:
                self.error('Unmatched quotation marks')
                raise e
        return self.data_

    def reply(self, msg, verbose=False):
        """Send a reply message.

        All plugin responses should be via this method.  The :attr:`user` is
        addressed by name if the response is in a channel rather than a private
        chat.  The reply is suppressed if the command was triggered indirectly
        (i.e. by command prefix instead of addressing the bot by name) and
        *verbose* is True.
        """
        if self.channel == self.bot.nickname:
            self.bot.msg(nick(self.user), msg)
        elif self.direct or not verbose:
            self.bot.msg(self.channel, msg)

    def error(self, err):
        """Send an error message."""
        self.reply('Error: ' + err, verbose=True)


class BotFactory(protocol.ClientFactory):
    def __init__(self, config, plugins, channels, command_prefix):
        self.config = config
        self.plugins = plugins
        self.channels = channels
        self.command_prefix = command_prefix

    def buildProtocol(self, addr):
        p = Bot(self.config, self.plugins)
        p.factory = self
        return p

    def clientConnectionLost(self, connector, reason):
        connector.connect()

    def clientConnectionFailed(self, connector, reason):
        reactor.stop()


def main(argv):
    import sys
    import argparse
    from straight.plugin import load

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', default='csbot.cfg',
                        help='Configuration file [default: %(default)s]')
    args = parser.parse_args(argv[1:])

    # Start twisted logging
    log.startLogging(sys.stdout)

    # Find plugins
    plugins = load('csbot.plugins', subclasses=Plugin)
    print "Plugins found:", plugins

    # Start client
    f = BotFactory(config=args.config,
                   plugins=plugins,
                   channels=['#cs-york-dev'],
                   command_prefix='!')
    reactor.connectTCP('irc.freenode.net', 6667, f)
    reactor.run()
