from functools import wraps
import types
import ConfigParser
import sys
import collections

from twisted.words.protocols import irc
from twisted.internet import reactor, protocol
from twisted.python import log
import straight.plugin
import pymongo

import csbot.events as events


class Bot(object):
    """The IRC bot.

    Handles plugins, command dispatch, hook dispatch, etc.  Persistent across
    losing and regaining connection.
    """

    #: Default configuration values
    DEFAULTS = {
            'nickname': 'csyorkbot',
            'username': 'csyorkbot',
            'realname': 'cs-york bot',
            'sourceURL': 'http://github.com/csyork/csbot/',
            'lineRate': '1',
            'keyvalfile': 'keyval.cfg',
            'irc_host': 'irc.freenode.net',
            'irc_port': '6667',
            'command_prefix': '!',
            'channels': ' '.join([
                '#cs-york-dev',
            ]),
            'plugins': ' '.join([
                'example',
            ]),
            'mongodb_host': 'localhost',
            'mongodb_port': '27017',
    }

    #: The top-level package for all bot plugins
    PLUGIN_PACKAGE = 'csbot.plugins'

    def __init__(self, configpath):
        # Load the configuration file
        self.configpath = configpath
        self.config = ConfigParser.SafeConfigParser(defaults=self.DEFAULTS,
                                                    allow_no_value=True)
        self.config.read(self.configpath)

        # Load plugin "key-value" store
        self.plugindata = ConfigParser.SafeConfigParser(allow_no_value=True)
        self.plugindata.read(self.config.get('DEFAULT', 'keyvalfile'))

        # Make mongodb connection
        self.mongodb = pymongo.Connection(
                self.config.get('DEFAULT', 'mongodb_host'),
                self.config.getint('DEFAULT', 'mongodb_port'))

        self.plugins = dict()
        self.commands = dict()

        # Event queue
        self.events = collections.deque()
        # Are we currently processing the event queue?
        self.events_running = False

    def setup(self):
        """Load plugins defined in configuration.
        """
        map(self.load_plugin, self.config.get('DEFAULT', 'plugins').split())

    def teardown(self):
        """Unload plugins and save data.
        """
        # Save currently loaded plugins
        self.config.set('DEFAULT', 'plugins', ' '.join(self.plugins))

        # Unload plugins
        for name in self.plugins.keys():
            self.unload_plugin(name)

        # Save the plugin data
        with open(self.config.get('DEFAULT', 'keyvalfile'), 'wb') as kvf:
            self.plugindata.write(kvf)

        # Save configuration
        with open(self.configpath, 'wb') as cfg:
            self.config.write(cfg)

    @classmethod
    def discover_plugins(cls):
        """Discover available plugins, returning a dictionary mapping from
        plugin name to plugin class.
        """
        plugins = straight.plugin.load(cls.PLUGIN_PACKAGE, subclasses=Plugin)
        available = dict()

        # Build dict of available plugins, error if there are multple plugins
        # with the same name
        for P in plugins:
            if P.plugin_name() in available:
                existing = available[P.plugin_name()]
                raise PluginError(('Duplicate plugin name: '
                        '{e.__module__}.{e.__name__} and '
                        '{n.__module__}.{n.__name__}').format(e=existing, n=P))
            else:
                available[P.plugin_name()] = P

        return available

    def has_plugin(self, name):
        """Check if the bot has the named plugin loaded.
        """
        return name in self.plugins

    def get_plugin(self, name):
        """Get a loaded plugin by name.
        """
        if name not in self.plugins:
            raise PluginError('{} not loaded'.format(name))

        return self.plugins[name]

    def load_plugin(self, name):
        """Load a named plugin and register all of its commands.

        When a plugin is loaded, it is added to the bot, all of its defined
        commands are registered, and then its :meth:`~Plugin.setup` is run.
        """
        available_plugins = self.discover_plugins()

        if name not in available_plugins:
            raise PluginError('{} does not exist'.format(name))

        if name in self.plugins:
            raise PluginError('{} already loaded'.format(name))

        p = available_plugins[name](self)
        self.plugins[name] = p
        self.log_msg('Loaded plugin {}'.format(name))

        for command, handler in p.features.commands.iteritems():
            if command in self.commands:
                raise PluginError('{} command already provided by {}'.format(
                    command, self.commands[command].im_class.plugin_name()))
            else:
                self.log_msg('Registering command {}'.format(command))
                self.commands[command] = handler

        p.setup()

    def unload_plugin(self, name):
        """Unload a named plugin and unregister all of its commands.

        When a plugin is unloaded, its :meth:'Plugin.teardown' method is run,
        all of its commands are unregistered, and then the plugin itself is
        removed from the :class:`Bot`.
        """
        if name not in self.plugins:
            raise PluginError('{} not loaded'.format(name))

        p = self.plugins[name]
        p.teardown()

        delcmds = [n for n, h in self.commands.iteritems()
                   if h.im_class.plugin_name() == name]
        for cmd in delcmds:
            self.log_msg('Unregistering command {}'.format(cmd))
            del self.commands[cmd]

        del self.plugins[name]
        self.log_msg('Unloaded plugin {}'.format(name))

    def reload_plugin(self, name):
        """Reload a named plugin, re-reading its source file.

        Attempts to :func:`reload` the source file containing the named plugin
        before unloading it and loading it again.
        """
        if name not in self.plugins:
            raise PluginError('{} not loaded'.format(name))

        # Reload the module this plugin came from, so that the next call to
        # discover_plugins() will get the newest code
        p = self.plugins[name]
        try:
            reload(sys.modules[p.__module__])
        except Exception as e:
            raise PluginError('reload failed', e)

        # Unload the plugin, unregistering all its commands etc.
        self.unload_plugin(name)
        # Load the plugin
        self.load_plugin(name)

    def post_event(self, event):
        """Post *event* into the bot event queue.

        The event is added to the queue, and if the queue isn't already being
        run then events start getting processed.  Usually all calls to this
        method would be done from inside a hook, so the event queue will be
        running and the newly added event will run shortly after the original
        event.
        """
        self.events.append(event)
        if not self.events_running:
            self.events_running = True
            while len(self.events) > 0:
                e = self.events.popleft()
                self.fire_hooks(e)
            self.events_running = False

    def fire_command(self, command):
        """Dispatch *command* to its callback.
        """
        if command.command not in self.commands:
            command.error('Command "{0.command}" not found'.format(command))
            return

        handler = self.commands[command.command]
        handler(command)

    def fire_hooks(self, event):
        """Fire hooks associated with ``event.event_type``.

        Firstly the :class:`Bot`'s hook for the event type is fired, followed
        by each plugin's hooks via :meth:`PluginFeatures.fire_hooks`.

        .. note:: The order that different plugins receive an event in is
                  undefined.
        """
        method = getattr(self, event.event_type, None)
        if method is not None:
            method(event)
        for plugin in self.plugins.itervalues():
            plugin.features.fire_hooks(event)

    def log_msg(self, msg):
        """Convenience wrapper around ``twisted.python.log.msg`` for plugins"""
        log.msg(msg)

    def log_err(self, err):
        """Convenience wrapper around ``twisted.python.log.err`` for plugins"""
        log.err(err)

    def signedOn(self, event):
        map(event.protocol.join,
            self.config.get('DEFAULT', 'channels').split())

    def privmsg(self, event):
        command = events.CommandEvent.create(event)
        if command is not None:
            self.post_event(command)

    def command(self, command_event):
        self.fire_command(command_event)


class PluginError(Exception):
    pass


class BotProtocol(irc.IRCClient):
    def __init__(self, bot):
        self.bot = bot
        # Get IRCClient configuration from the Bot
        self.nickname = bot.config.get('DEFAULT', 'nickname')
        self.username = bot.config.get('DEFAULT', 'username')
        self.realname = bot.config.get('DEFAULT', 'realname')
        self.sourceURL = bot.config.get('DEFAULT', 'sourceURL')
        self.lineRate = bot.config.getint('DEFAULT', 'lineRate')

        # Keeps partial name lists between RPL_NAMREPLY and
        # RPL_ENDOFNAMES events
        self.names_accumulator = dict()

    def connectionMade(self):
        irc.IRCClient.connectionMade(self)
        print "[Connected]"

    def connectionLost(self, reason):
        irc.IRCClient.connectionLost(self, reason)
        print "[Disconnected because {}]".format(reason)

    @events.proxy
    def signedOn(self):
        pass

    @events.proxy
    def privmsg(self, user, channel, message):
        pass

    @events.proxy
    def noticed(self, user, channel, message):
        pass

    @events.proxy
    def action(self, user, channel, message):
        pass

    @events.proxy
    def joined(self, channel):
        pass

    @events.proxy
    def left(self, channel):
        pass

    @events.proxy
    def userJoined(self, user, channel):
        pass

    @events.proxy
    def userLeft(self, user, channel):
        pass

    @events.proxy
    def userQuit(self, user, message):
        pass

    @events.proxy
    def names(self, channel, names, raw_names):
        """Called when the NAMES list for a channel has been received.
        """
        pass

    def irc_RPL_NAMREPLY(self, prefix, params):
        channel = params[2]
        names = self.names_accumulator.get(channel, list())
        names.extend(params[3].split())
        self.names_accumulator[channel] = names

    def irc_RPL_ENDOFNAMES(self, prefix, params):
        # Get channel and raw names list
        channel = params[1]
        raw_names = self.names_accumulator.pop(channel, list())

        # Get a mapping from status characters to mode flags
        prefixes = self.supported.getFeature('PREFIX')
        inverse_prefixes = dict((v[0], k) for k, v in prefixes.iteritems())

        # Get mode characters from name prefix
        def f(name):
            if name[0] in inverse_prefixes:
                return (name[1:], set(inverse_prefixes[name[0]]))
            else:
                return (name, set())
        names = map(f, raw_names)

        # Fire the event
        self.names(channel, names, raw_names)


class PluginFeatures(object):
    """Utility class to simplify defining plugin features.

    Plugins can define hooks and commands.  This class provides a
    decorator-based approach to creating these features.
    """
    def __init__(self):
        self.commands = dict()
        self.hooks = dict()

    def instantiate(self, inst):
        """Create a duplicate :class:`PluginFeatures` bound to *inst*.

        Returns an exact duplicate of this object, but every method that has
        been registered with a decorator is bound to *inst* so when it's called
        it acts like a normal method call.
        """
        cls = inst.__class__
        features = PluginFeatures()
        features.commands = dict((c, types.MethodType(f, inst, cls))
                                 for c, f in self.commands.iteritems())
        features.hooks = dict((h, [types.MethodType(f, inst, cls) for f in fs])
                              for h, fs in self.hooks.iteritems())
        return features

    def hook(self, hook):
        """Create a decorator to register a handler for *hook*.
        """
        if hook not in self.hooks:
            self.hooks[hook] = list()

        def decorate(f):
            self.hooks[hook].append(f)
            return f
        return decorate

    def command(self, command, help=None):
        """Create a decorator to register a handler for *command*.

        Raises a :class:`KeyError` if this class has already registered a
        handler for *command*.
        """
        if command in self.commands:
            raise KeyError('Duplicate command: {}'.format(command))

        def decorate(f):
            f.help = help
            self.commands[command] = f
            return f
        return decorate

    def fire_hooks(self, event):
        """Fire plugin hooks associated with ``event.event_type``.

        Hook handlers are run in the order they were registered, which should
        correspond to the order they were defined if decorators were used.
        """
        hooks = self.hooks.get(event.event_type, list())
        for h in hooks:
            h(event)


class Plugin(object):
    """Bot plugin base class.
    """

    features = PluginFeatures()

    def __init__(self, bot):
        self.bot = bot
        self.features = self.features.instantiate(self)
        self.db_ = None

    @classmethod
    def plugin_name(cls):
        """Get the plugin's name.

        A plugin's name is its class name in lowercase.  Duplicate plugin names
        are not permitted and plugin names should be handled case-insensitively
        as ``name.lower()``.

        >>> from csbot.plugins.example import EmptyPlugin
        >>> EmptyPlugin.plugin_name()
        'emptyplugin'
        >>> p = EmptyPlugin(None)
        >>> p.plugin_name()
        'emptyplugin'
        """
        return cls.__name__.lower()

    @property
    def db(self):
        if self.db_ is None:
            self.db_ = self.bot.mongodb['csbot__' + self.plugin_name()]
        return self.db_

    def cfg(self, name):
        plugin = self.plugin_name()

        # Check plugin config
        if self.bot.config.has_section(plugin):
            if self.bot.config.has_option(plugin, name):
                return self.bot.config.get(plugin, name)

        # Check default config
        if self.bot.config.has_option("DEFAULT", name):
            return self.bot.config.get("DEFAULT", name)

        # Raise an exception
        raise KeyError("{} is not a valid option.".format(name))

    def get(self, key):
        """Get a value from the plugin key/value store by key. If the key
        is not found, a KeyError is raised.
        """

        plugin = self.plugin_name()

        if self.bot.plugindata.has_section(plugin):
            if self.bot.plugindata.has_option(plugin, key):
                return self.bot.plugindata.get(plugin, key)

        raise KeyError("{} is not defined.".format(key))

    def set(self, key, value):
        """Set a value in the plugin key/value store by key.
        """

        plugin = self.plugin_name()

        if not self.bot.plugindata.has_section(plugin):
            self.bot.plugindata.add_section(plugin)

        self.bot.plugindata.set(plugin, key, value)

    def setup(self):
        """Run setup actions for the plugin.

        This should be overloaded in plugins to perform actions that need to
        happen before receiving any events.

        .. note:: Plugin setup order is not guaranteed to be consistent, so do
                  not rely on it.
        """
        pass

    def teardown(self):
        """Run teardown actions for the plugin.

        This should be overloaded in plugins to perform teardown actions, for
        example writing stuff to file/database, before the bot is destroyed.

        .. note:: Plugin teardown order is not guaranteed to be consistent, so
                  do not rely on it.
        """
        pass


class BotFactory(protocol.ClientFactory):
    def __init__(self, bot):
        self.bot = bot

    def buildProtocol(self, addr):
        p = BotProtocol(self.bot)
        p.factory = self
        return p

    def clientConnectionLost(self, connector, reason):
        connector.connect()

    def clientConnectionFailed(self, connector, reason):
        reactor.stop()


def main(argv):
    import sys
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', default='csbot.cfg',
                        help='Configuration file [default: %(default)s]')
    args = parser.parse_args(argv[1:])

    # Start twisted logging
    log.startLogging(sys.stdout)

    # Create bot and run setup functions
    bot = Bot(args.config)
    bot.setup()

    # Connect and enter the reactor loop
    reactor.connectTCP(bot.config.get('DEFAULT', 'irc_host'),
                       bot.config.getint('DEFAULT', 'irc_port'),
                       BotFactory(bot))
    reactor.run()

    # Run teardown functions before exiting
    bot.teardown()
