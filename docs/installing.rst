Installing
==========

Minimum Python Version
----------------------

The minimum Python version supported is 3.8.

Lavalink Compatibility
----------------------

Mafic 2.x retains Lavalink 3.7 compatibility. Its Lavalink v4 protocol support is
tested against stable Lavalink 4.2.2. Newer Lavalink minor versions may introduce
client-visible fields and will produce :class:`~mafic.UnsupportedVersionWarning`
until Mafic has been audited against them.

Installing Mafic
----------------

Mafic is on PyPI, so it can be installed via pip. The command varies depending on your
system, generally use what you used to install your Discord library.

.. tab:: Windows

   .. code-block:: powershell

      > py -m pip install mafic

.. tab:: MacOS / Linux

   .. code-block:: sh

      $ python3 -m pip install mafic
