.. currentmodule:: mafic

Node Management
===============

This file is about managing nodes and selecting the best node for a player.

.. autodata:: mafic.type_variables.ClientT

.. attributetable:: NodePool

.. autoclass:: NodePool

``NodePool.label_to_node`` is a read-only live view. Use ``add_node()`` and
``remove_node()`` to change the nodes owned by the pool.

.. autofunction:: pool_for

To recover a pool after losing the application's reference, call
``pool_for(client)``. A failed or still-running close keeps that pool bound to
the client. Inspect ``pool.closing``, ``pool.failed_nodes`` and
``pool.managed_nodes`` before retrying ``pool.close()``.

If an in-flight operation cannot finish, an operator can close its node from
``pool.managed_nodes`` to interrupt its internally owned connection or HTTP
session. A caller-supplied session remains the caller's responsibility.
Interrupting a request may leave its remote outcome unknown. If an operation
cannot be interrupted safely, restarting the process is the final recovery
option; Mafic keeps the binding while cleanup remains unfinished. A close that
is still running may be observed through a separately retained, shielded task;
a caller-side timeout does not release the client or stop that close.

.. autodata:: mafic.pool.StrategyList

.. autodata:: mafic.strategy.StrategyCallable

.. autoclass:: Strategy

.. autoclass:: VoiceRegion
   :undoc-members:

.. autoclass:: Region
   :undoc-members:

.. autoclass:: Group
   :undoc-members:
