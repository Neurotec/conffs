# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""
Config management API and helpers.
"""
import time
import logging
import os
from lxml import etree
from plumbum import ProcessExecutionError
from .utils import RestoreFile
from .orms import buildfromschema
from .schema import _sections, _apis


log = logging.getLogger('conffs')


class CLIError(Exception):
    '''`fs_cli` command error'''


class CLIConnectionError(CLIError):
    '''Failed to connect to local ESL on `fs_cli` commands'''


class cli(object):
    """``fs_cli`` wrapper which quacks like a func and can raise
    errors based on string handlers.
    """
    CLIError = CLIError
    CLIConnectionError = CLIConnectionError

    def __init__(self, shell, *args, **kwargs):
        self.shell = shell
        prefix = kwargs.pop('prefix', None)
        self.prefix = prefix
        self._cmd = self.get_cmd(prefix=prefix, *args, **kwargs)

    def get_cmd(self, *args, **kwargs):
        """Build and return an ``fs_cli`` command instance.
        """
        cli = self.shell
        prefix = kwargs.pop('prefix', None)
        if prefix:
            for item in prefix:
                cli = cli[item]

        cli = cli['fs_cli']
        for arg in args:
            cli = cli['--{}'.format(arg)]

        for key, value in kwargs.items():
            cli = cli['--{}={}'.format(key, value)]

        return cli['-x']

    def __call__(self, *tokens, **erroron):
        '''Invoke an `fs_cli` command and return output with error handling.
        '''
        try:
            out = self._cmd(' '.join(map(str, tokens))).strip()
        except ProcessExecutionError as err:
            raise CLIConnectionError(str(err))

        lines = out.splitlines()
        if lines and lines[-1].startswith('-ERR'):
            raise CLIError(out)

        if erroron:
            for name, func in erroron.items():
                if func(out):
                    raise CLIError(out)
        return out

    def eval(self, expr):
        """Eval an expression in ``fs_cli``.
        """
        return self('eval', expr)


class Client(object):
    '''Manages a collection of restorable XML objects discovered in the
    FreeSWITCH config directories.
    '''
    def __init__(self, rfile, etree, conf_io, fscli, log):
        self.etree = etree
        self.root = etree.getroot()
        self.file = rfile
        self.conf_io = conf_io
        self.cli = fscli
        self.log = log
        self._touched = []

    @property
    def fscli(self):
        """Alias for self.cli
        """
        return self.cli

    def revert(self):
        """Revert all changes to the root config file.
        """
        self.log.debug("restoring '{}'".format(self.file.path))
        self.file.restore()

    def commit(self):
        """Commit the working XML etree to the remote FreeSWITCH config.
        """
        now = time.time()
        self.log.info("saving '{}'".format(self.file.path))
        self.conf_io.write(etree.tostring(self.etree, pretty_print=True))
        self.fscli('reloadxml')
        log.debug("freeswitch.xml commit took {} seconds"
                  .format(time.time() - now))


def manage_config(confpath, conf_io, fscli, log, singlefile=True):
    """Manage the FreeSWITCH configuration found at ``rootpath`` or as
    auto-discovered using ``fs_cli`` over ssh. By default all XML configs are
    squashed down to a single ``freeswitch.xml`` file.
    """
    parser = etree.XMLParser(remove_blank_text=True)

    start = time.time()
    with conf_io.open() as fxml:
        tree = etree.parse(fxml, parser)
    log.info("Parsing {} into an etree took {} seconds".format(
        confpath, time.time() - start))

    # check if this is a single-file config by trying to access the
    # sofia configuration section
    if not tree.xpath('section/configuration[@name="sofia.conf"]'):
        log.info("Dumping aggregate freeswitch.xml config...")
        # parse the single-document config
        root = etree.fromstring(fscli('xml_locate', 'root'), parser=parser)
        tree = etree.ElementTree(root)

        # remove all ignored whitespace from tail text since FS doesn't
        # use element text whatsoever
        for elem in root.iter():
            elem.tail = None
            if elem.text is not None:
                elem.text = os.linesep.join(
                    (s for s in elem.text.splitlines() if s.strip()))

        # back up original freeswitch.xml root config
        backup = confpath + time.strftime('_backup_%Y-%m-%d-%H-%M-%S')
        log.info("Backing up old {} as {}...".format(confpath, backup))
        conf_io.copy(backup)

        with conf_io.open('w') as fxml:
            fxml.write(etree.tostring(tree, pretty_print=True).decode())

    client = Client(
        RestoreFile(confpath, open=conf_io.open),
        tree, conf_io, fscli, log
    )

    class Api(object):
        "Module api placeholder"
        def __init__(self, client):
            self.client = client

    # apply section mapped apis/models as attrs
    for f, cls in _sections:
        modname = cls.modeldata['modname']
        api = _apis.get(modname)
        if api:
            module = api(client)
        else:
            module = Api(client)

        setattr(client, modname, module)
        section = buildfromschema(
            cls, getattr(cls, 'schema', None), root=client.root,
            log=log, client=client
        )
        setattr(module, 'config', section)

    return client
