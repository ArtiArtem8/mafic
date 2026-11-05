r"""A module containing a :class:`NodePool`, used to manage :class:`Node`\s."""

# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from functools import partial
from logging import getLogger
from random import choice
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, Sequence, Union
from weakref import WeakKeyDictionary, ref

from .errors import NodePoolAlreadyBound, NoNodesAvailable, PlayerNotConnected
from .node import Node
from .strategy import Strategy, StrategyCallable, call_strategy
from .type_variables import ClientT
from .utils.tasks import await_owned

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping
    from weakref import ReferenceType

    import aiohttp

    from .player import Player
    from .region import Group, Region, VoiceRegion


__all__ = ("NodePool", "pool_for")

_log = getLogger(__name__)
_CLIENT_POOL_ATTRIBUTE = "_mafic_node_pool"

_pools: WeakKeyDictionary[object, ReferenceType[NodePool[Any]]] = WeakKeyDictionary()
"""Active pool per Discord client.

Both registry references are weak. The client retains its pool until a
successful close, without a global strong reference to either object.
"""


def pool_for(client: object) -> NodePool[Any] | None:
    """Return the active pool bound to one Discord client, if any.

    Discord constructs a player through ``cls(client, channel)``, so this is
    how a player finds the node scope that owns its client.
    """
    entry = _pools.get(client)
    return entry() if entry is not None else None


StrategyList = Union[
    StrategyCallable[ClientT],
    Sequence[Strategy],
    Sequence[StrategyCallable[ClientT]],
    Sequence[Union[Strategy, StrategyCallable[ClientT]]],
]
"""A type hint for a list of strategies to select the best node.

This can either be:

- A single :data:`.StrategyCallable`.
- A sequence of :class:`.Strategy`\\s.
- A sequence of :data:`.StrategyCallable`\\s.
- A sequence of :class:`.Strategy`\\s and :data:`.StrategyCallable`\\s.
"""


class NodePool(Generic[ClientT]):
    """A class that manages nodes and chooses them based on strategies.

    The client retains its pool until successful close. Use :func:`pool_for`
    to recover the pool if the application loses its own reference.

    Parameters
    ----------
    client:
        The client to use to connect to the nodes.
    default_strategies:
        The default strategies to use when selecting a node. Defaults to
        [:attr:`Strategy.SHARD`, :attr:`Strategy.LOCATION`, :attr:`Strategy.USAGE`].
    """

    __slots__ = (
        "__weakref__",
        "_client",
        "_close_task",
        "_default_strategies",
        "_failed_nodes",
        "_nodes",
        "_pending_labels",
        "_pending_operations",
        "_remove_tasks",
    )

    def __init__(
        self,
        client: ClientT,
        default_strategies: StrategyList[ClientT] | None = None,
    ) -> None:
        existing = pool_for(client)
        if existing is not None:
            msg = f"Client {client!r} already has an active NodePool."
            raise NodePoolAlreadyBound(msg)
        self._client: ClientT = client
        self._close_task: asyncio.Task[None] | None = None
        # Per pool: independent Discord clients must never share nodes.
        self._nodes: dict[str, Node[ClientT]] = {}
        self._failed_nodes: set[Node[ClientT]] = set()
        self._pending_labels: set[str] = set()
        self._pending_operations: dict[
            asyncio.Future[None], tuple[asyncio.Task[Any], Node[ClientT]]
        ] = {}
        # Identity, not label: a replacement with the same label is a new node.
        self._remove_tasks: dict[Node[Any], asyncio.Task[None]] = {}
        self._default_strategies: StrategyList[ClientT] = (
            [
                Strategy.SHARD,
                Strategy.LOCATION,
                Strategy.USAGE,
            ]
            if default_strategies is None
            else default_strategies
        )
        self._bind_client()

    @property
    def label_to_node(self) -> Mapping[str, Node[ClientT]]:
        """A read-only view of node labels and their registered nodes."""
        return MappingProxyType(self._nodes)

    @property
    def nodes(self) -> list[Node[ClientT]]:
        """Get the list of all available nodes."""
        return [node for node in self._nodes.values() if node.available]

    @property
    def closing(self) -> bool:
        """Whether the pool is currently waiting for or closing its nodes."""
        return self._close_task is not None

    @property
    def managed_nodes(self) -> tuple[Node[ClientT], ...]:
        """Nodes registered, in flight, or retained after a cleanup failure.

        This snapshot includes nodes already removed from :attr:`label_to_node`.
        It allows an operator to inspect a stuck shutdown and, if necessary,
        close an owned node to interrupt its connection or HTTP request when possible.
        Caller-owned HTTP sessions remain the caller's responsibility, and a
        cancellation-resistant operation may require restarting the process.
        """
        nodes = list(self._nodes.values())
        nodes.extend(node for _, node in self._pending_operations.values())
        nodes.extend(self._failed_nodes)
        return tuple(dict.fromkeys(nodes))

    @property
    def failed_nodes(self) -> tuple[Node[ClientT], ...]:
        """Nodes whose last cleanup did not complete successfully."""
        return tuple(self._failed_nodes)

    async def create_node(
        self,
        *,
        host: str,
        port: int,
        label: str,
        password: str,
        secure: bool = False,
        heartbeat: int = 30,
        timeout: float = 10,
        http_timeout: aiohttp.ClientTimeout | None = None,
        session: aiohttp.ClientSession | None = None,
        resume_key: str | None = None,
        regions: Sequence[Group | Region | VoiceRegion] | None = None,
        shard_ids: Sequence[int] | None = None,
        resuming_session_id: str | None = None,
        player_cls: type[Player[ClientT]] | None = None,
    ) -> Node[ClientT]:
        r"""Create a node and connect it.

        The parameters here relate to :class:`Node`.

        Parameters
        ----------
        host:
            The host of the node to connect to.
        port:
            The port of the node to connect to.
        label:
            The label of the node used to identify it.
        password:
            The password of the node to authenticate.
        secure:
            Whether to use SSL (TLS) or not.
        heartbeat:
            The interval to send heartbeats to the node websocket connection.
        timeout:
            The timeout to use for the node websocket connection.
        http_timeout: :data:`~typing.Optional`\[:class:`aiohttp.ClientTimeout`]
            The timeout applied to internally created HTTP sessions. ``None``
            uses aiohttp's session defaults, including ``sock_connect=30``.
            Ignored when ``session`` is supplied.
        session: :data:`~typing.Optional`\[:class:`aiohttp.ClientSession`]
            The session to use for the node websocket connection.
            The caller retains ownership of a supplied session.
        resume_key:
            The key to use when resuming the node.
            If not provided, the key will be generated from the host, port and label.

            .. warning::

                This is ignored in lavalink V4, use ``resuming_session_id`` instead.
        regions:
            The voice regions that the node can be used in.
            This is used to determine when to use this node.
        shard_ids:
            The shard IDs that the node can be used in.
            This is used to determine when to use this node.
        resuming_session_id:
            The session ID to use when resuming the node.
            If not provided, the node will not resume.

            This should be stored from :func:`~mafic.on_node_ready` with
            :attr:`session_id` to resume the session and gain control of the players.
            If the node is not resuming, players will be destroyed if Lavalink loses
            connection to us.

            .. versionadded:: 2.2
        player_cls:
            The player class to use for this node when resuming.

            .. versionadded:: 2.8

        Returns
        -------
        :class:`Node`
            The created node.

        Raises
        ------
        ValueError
            If the label is already used by this pool.
        NodePoolAlreadyBound
            If another pool now owns this client.
        RuntimeError
            If the pool is closing.
        """
        self._check_client_scope()
        self._check_accepting_nodes()
        node = Node(
            host=host,
            port=port,
            label=label,
            password=password,
            client=self._client,
            secure=secure,
            heartbeat=heartbeat,
            timeout=timeout,
            http_timeout=http_timeout,
            session=session,
            resume_key=resume_key,
            regions=regions,
            shard_ids=shard_ids,
            resuming_session_id=resuming_session_id,
        )
        self._bind_client()
        with self._track_operation(node):
            try:
                await self.add_node(node, player_cls=player_cls)
            except BaseException:
                # The unreturned node remains our responsibility on failure.
                try:
                    await node.close()
                except BaseException as error:
                    self._failed_nodes.add(node)
                    if not isinstance(error, Exception):
                        raise
                    _log.exception(
                        "Failed to close node after an unsuccessful connection.",
                        extra={"label": node.label},
                    )
                raise
            return node

    async def add_node(
        self, node: Node[ClientT], *, player_cls: type[Player[ClientT]] | None = None
    ) -> None:
        """Add an existing node to this pool.

        .. note::

            You generally do not want this, use :meth:`create_node` instead.
            This is used for after running :meth:`remove_node` to re-add the node
            if it has been restarted.

            If connecting fails, the caller remains responsible for closing this
            node. :meth:`create_node` closes the node it creates on failure.

        .. versionadded:: 2.7

        Parameters
        ----------
        node:
            The node to add.
        player_cls:
            The player class to use for this node when resuming.

            .. versionadded:: 2.8

        Raises
        ------
        ValueError
            If the node belongs to another client, another node uses its label,
            or a node with that label is already connecting.
        NodeAlreadyConnected
            If this node is already connected or connecting.
        NodePoolAlreadyBound
            If another pool now owns this client.
        RuntimeError
            If the pool is closing.
        """
        self._check_node_client(node)
        self._check_accepting_nodes()
        self._bind_client()
        label = node.label
        if label in self._pending_labels or (
            label in self._nodes and self._nodes[label] is not node
        ):
            msg = f"Node label {label!r} is already in use by this pool."
            raise ValueError(msg)

        _log.info("Created node, connecting it...", extra={"label": node.label})
        self._pending_labels.add(label)
        with self._track_operation(node):
            try:
                await node.connect(player_cls=player_cls)
                if label in self._nodes and self._nodes[label] is not node:
                    # Do not overwrite a different node registered meanwhile.
                    await node.close()
                    msg = f"Node label {label!r} is already in use by this pool."
                    raise ValueError(msg)
                self._nodes[label] = node
                self._failed_nodes.discard(node)
            finally:
                self._pending_labels.discard(label)

    @contextmanager
    def _track_operation(self, node: Node[ClientT]) -> Generator[None, None, None]:
        """Track the operation and its node, not the caller's entire task."""
        owner = asyncio.current_task()
        if owner is None:
            msg = "NodePool operations require an asyncio task."
            raise RuntimeError(msg)
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending_operations[done] = (owner, node)
        try:
            yield
        finally:
            del self._pending_operations[done]
            done.set_result(None)

    def _check_client_scope(self) -> None:
        """Reject an operation after another pool has claimed this client."""
        existing = pool_for(self._client)
        if existing is not None and existing is not self:
            msg = f"Client {self._client!r} already has an active NodePool."
            raise NodePoolAlreadyBound(msg)

    def _bind_client(self) -> None:
        """Bind this pool again if it was closed and the client remains free."""
        self._check_client_scope()
        client_attributes = vars(self._client)
        if (
            _CLIENT_POOL_ATTRIBUTE in client_attributes
            and client_attributes[_CLIENT_POOL_ATTRIBUTE] is not self
        ):
            msg = f"Client attribute {_CLIENT_POOL_ATTRIBUTE!r} is already in use."
            raise RuntimeError(msg)
        setattr(self._client, _CLIENT_POOL_ATTRIBUTE, self)
        _pools[self._client] = ref(self)

    def _check_accepting_nodes(self) -> None:
        """Do not admit node changes during a pool close."""
        if self._close_task is not None:
            msg = "NodePool is closing."
            raise RuntimeError(msg)

    def _check_node_client(self, node: Node[ClientT]) -> None:
        """Reject nodes owned by another Discord client."""
        if node.client is not self._client:
            msg = "Node belongs to a different client than this pool."
            raise ValueError(msg)

    async def remove_node(
        self, node: Node[ClientT] | str, *, transfer_players: bool = True
    ) -> None:
        """Remove a node from the pool.

        The node leaves the pool before player teardown. Every player operation
        is attempted even when another player fails, and the node is always
        closed afterwards. The transport is quiesced before the player snapshot,
        so REST stays available for transfers and deletes while no reconnect can
        register another player into an already-removed node. Concurrent calls
        for the same node instance share one owned operation; a caller's
        cancellation is propagated only after it settles. The first caller's
        ``transfer_players`` value is used for that operation. A replacement
        node registered under the same label is a different instance and gets
        its own removal.

        A successful transfer keeps the target player. A failed transfer tears
        down only the original player if it still belongs to the removed node.
        A replaced Discord voice client is never disconnected by this method.

        .. versionadded:: 2.6

        Parameters
        ----------
        node:
            The node to remove.
        transfer_players:
            Whether to transfer players to other nodes or disconnect them.

        Raises
        ------
        ValueError
            If the node belongs to another client.
        NodePoolAlreadyBound
            If another pool now owns this client.
        RuntimeError
            If the pool is closing.
        """
        self._check_client_scope()
        self._check_accepting_nodes()
        resolved = self._nodes[node] if isinstance(node, str) else node
        self._check_node_client(resolved)
        self._bind_client()
        with self._track_operation(resolved):
            task = self._remove_tasks.get(resolved)
            if task is None:
                task = asyncio.create_task(
                    self._remove_node(resolved, transfer_players=transfer_players)
                )
                self._remove_tasks[resolved] = task
                task.add_done_callback(partial(self._forget_removal, resolved))
            await await_owned(task)

    def _forget_removal(self, node: Node[Any], task: asyncio.Task[None]) -> None:
        """Forget a finished removal only when it is still the registered one."""
        if self._remove_tasks.get(node) is task:
            self._remove_tasks.pop(node, None)

    async def _remove_node(
        self, node: Node[ClientT], *, transfer_players: bool
    ) -> None:
        """Own one removal from pool deletion through node close."""
        if self._nodes.get(node.label) is node:
            self._nodes.pop(node.label, None)
        try:
            # Stop transport first so no reconnect can resurrect players
            # while this removal tears them down.
            await node.quiesce()
            # Snapshot after quiesce: a sync that completed while the
            # transport was stopping may have registered new players.
            players = tuple(node.players)
            if transfer_players:
                await self._transfer_players(node, players)
            else:
                await self._disconnect_players(node, players)
        finally:
            try:
                await node.close()
            except BaseException:
                self._failed_nodes.add(node)
                raise
            else:
                self._failed_nodes.discard(node)

    async def _transfer_players(
        self, node: Node[ClientT], players: Sequence[Player[ClientT]]
    ) -> None:
        """Attempt every transfer, cleaning up only players left on the node."""

        async def transfer(player: Player[ClientT]) -> None:
            if player.assigned_node is not node:
                await self._disconnect_player(node, player)
                return
            try:
                target = self.get_node(
                    guild_id=player.guild.id,
                    endpoint=player.endpoint,  # pyright: ignore[reportPrivateUsage]
                )
                transferred = await player.transfer_to(target, expected_source=node)
                if not transferred:
                    await self._disconnect_player(node, player)
                    return
            except (RuntimeError, NoNodesAvailable, PlayerNotConnected):
                _log.error(
                    "Failed to transfer player %d from removed node %s.",
                    player.guild.id,
                    node.label,
                    exc_info=True,
                )
                await self._disconnect_player(node, player)
            except Exception:  # noqa: BLE001 - one player must not stop removal
                _log.exception(
                    "Unexpected player transfer failure from removed node %s.",
                    node.label,
                )
                await self._disconnect_player(node, player)

        results = await asyncio.gather(
            *(transfer(player) for player in players),
            return_exceptions=True,
        )
        self._log_failed_teardowns(node, results)

    async def _disconnect_players(
        self, node: Node[ClientT], players: Sequence[Player[ClientT]]
    ) -> None:
        """Attempt every disconnect without letting one failure stop others."""
        results = await asyncio.gather(
            *(self._disconnect_player(node, player) for player in players),
            return_exceptions=True,
        )
        self._log_failed_teardowns(node, results)

    async def _disconnect_player(
        self, node: Node[ClientT], player: Player[ClientT]
    ) -> None:
        """Tear down one player only while it still owns the removed node."""
        owner = player.assigned_node
        if owner is not node and owner is not None:
            node.discard_player(player)
            return
        try:
            if player.disconnect_started:
                await player.wait_for_disconnect()
                return
            if owner is not node or player.guild.voice_client is not player:
                return
            await player.disconnect(force=True)
        finally:
            node.discard_player(player)

    @staticmethod
    def _log_failed_teardowns(node: Node[ClientT], results: Sequence[object]) -> None:
        """Report failures that survived all-player teardown."""
        for result in results:
            if isinstance(result, BaseException):
                _log.error(
                    "Failed to tear down a player on removed node %s.",
                    node.label,
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def close(self) -> None:
        """Close all nodes in the pool and release its client scope.

        Concurrent calls share one operation. Every node is asked to close,
        even if another fails. Caller cancellation is propagated only after
        the operation settles. On failure, the client stays bound to this pool
        so closing can be retried; on success a new pool may be created.

        In-flight node creation, addition and removal finish before nodes close;
        new node operations are rejected while this happens. An in-flight
        network request or retry may delay shutdown indefinitely. To bound only
        the caller's wait, run close as a task and shield it inside a timeout.
        Keep that task and observe its result: a timeout does not release the
        client or stop the owned shutdown.
        Successful close clears the node mapping. The pool may be reused if no
        newer pool has claimed the client.

        A callback awaited by a node operation must not await this pool's close,
        including through another task: close itself waits for that operation.

        Raises
        ------
        NodePoolAlreadyBound
            If another pool now owns this client.
        RuntimeError
            If called directly from this pool's own active operation or close
            callback, which would otherwise wait for itself.

        .. versionadded:: 2.6
        """
        caller = asyncio.current_task()
        if caller is not None and (
            any(owner is caller for owner, _ in self._pending_operations.values())
            or caller in self._remove_tasks.values()
            or caller is self._close_task
        ):
            msg = "Cannot close NodePool from one of its own operations."
            raise RuntimeError(msg)
        self._check_client_scope()
        task = self._close_task
        if task is None:
            task = self._close_task = asyncio.create_task(self._close())
        await await_owned(task)

    async def _close(self) -> None:
        """Close every node and release the client only after success."""
        try:
            pending = tuple(self._pending_operations)
            if pending:
                await asyncio.wait(pending)

            nodes = self.managed_nodes
            results = await asyncio.gather(
                *(self._close_node(node) for node in nodes), return_exceptions=True
            )
            failure: BaseException | None = None
            for node, result in zip(nodes, results, strict=True):
                if isinstance(result, BaseException):
                    self._failed_nodes.add(node)
                    _log.error(
                        "Failed to close node %s.",
                        node.label,
                        exc_info=(type(result), result, result.__traceback__),
                    )
                    if failure is None:
                        failure = result
                else:
                    self._failed_nodes.discard(node)
            if failure is not None:
                raise failure

            for node in nodes:
                if self._nodes.get(node.label) is node:
                    self._nodes.pop(node.label, None)
            entry = _pools.get(self._client)
            if entry is not None and entry() is self:
                if vars(self._client).get(_CLIENT_POOL_ATTRIBUTE) is self:
                    delattr(self._client, _CLIENT_POOL_ATTRIBUTE)
                del _pools[self._client]
        finally:
            self._close_task = None

    async def _close_node(self, node: Node[ClientT]) -> None:
        """Close one node, rejecting a direct recursive close callback."""
        with self._track_operation(node):
            await node.close()

    def get_node(
        self,
        *,
        guild_id: str | int,
        endpoint: str | None,
        strategies: StrategyList[ClientT] | None = None,
    ) -> Node[ClientT]:
        """Get a node based on the given strategies.

        Parameters
        ----------
        guild_id:
            The guild ID to get a node for.
        endpoint:
            The endpoint to get a node for.
        strategies:
            The strategies to use to get a node. If not provided, the default
            strategies will be used.

        Returns
        -------
        :class:`Node`
            The node to use.

        Raises
        ------
        NoNodesAvailable
            If no nodes remain after selection.
        """
        actual_strategies: Sequence[StrategyCallable[ClientT] | Strategy]

        strategies = strategies or self._default_strategies

        actual_strategies = [strategies] if callable(strategies) else strategies

        nodes = self.nodes

        for strategy in actual_strategies:
            if isinstance(strategy, Strategy):
                strategy_callable = partial(call_strategy, strategy)
            else:
                strategy_callable = strategy

            nodes = strategy_callable(
                nodes, int(guild_id), self._client.shard_count, endpoint
            )

            _log.debug(
                "Strategy %s returned nodes %s.",
                strategy.__name__ if callable(strategy) else strategy.name,
                ", ".join(n.label for n in nodes),
            )

            if len(nodes) == 1:
                return nodes[0]
            if not nodes:
                raise NoNodesAvailable

        if not nodes:
            raise NoNodesAvailable
        return choice(nodes)

    def get_random_node(self) -> Node[ClientT]:
        """Get a random node.

        Returns
        -------
        :class:`Node`
            The random node.

        Raises
        ------
        NoNodesAvailable
            If there are no available nodes.
        """
        nodes = self.nodes

        if not nodes:
            raise NoNodesAvailable
        return choice(nodes)
