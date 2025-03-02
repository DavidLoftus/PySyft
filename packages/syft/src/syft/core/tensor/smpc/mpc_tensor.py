# future
from __future__ import annotations

# stdlib
import functools
import itertools
import secrets
from typing import Any
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

# third party
import numpy as np
import numpy.typing as npt
import torch

# relative
from . import utils
from .... import logger
from ....ast.klass import get_run_class_method
from ....grid import GridURL
from ...smpc.protocol.spdz import spdz
from ..passthrough import PassthroughTensor  # type: ignore
from ..passthrough import SupportedChainType  # type: ignore
from ..util import implements  # type: ignore
from .share_tensor import ShareTensor

METHODS_FORWARD_ALL_SHARES = {
    "repeat",
    "copy",
    "diagonal",
    "flatten",
    "transpose",
    "partition",
    "resize",
    "ravel",
    "compress",
    "reshape",
    "squeeze",
    "swapaxes",
    "sum",
    "__pos__",
    "__neg__",
    "take",
    "choose",
    "cumsum",
    "trace",
}
INPLACE_OPS = {
    "resize",
}

PARTIES_REGISTER_CACHE: Dict[Any, GridURL] = {}


class MPCTensor(PassthroughTensor):
    __slots__ = (
        "seed_przs",
        "mpc_shape",
        "parties",
        "parties_info",
        "ring_size",
    )

    def __init__(
        self,
        parties: List[Any],
        secret: Optional[Any] = None,
        shares: Optional[List[ShareTensor]] = None,
        shape: Optional[Tuple[int, ...]] = None,
        seed_przs: Optional[int] = None,
        ring_size: Optional[int] = None,
    ) -> None:

        if secret is None and shares is None:
            raise ValueError("Secret or shares should be populated!")

        if (shares is not None) and (not isinstance(shares, (tuple, list))):
            raise ValueError("Shares should be a list or tuple")

        if seed_przs is None:
            # Allow the user to specify if they want to use a specific seed when generating the shares
            # ^This is unsecure and should be used with cautioness
            seed_przs = secrets.randbits(32)

        self.seed_przs = seed_przs
        self.parties = parties
        self.parties_info = MPCTensor.get_parties_info(parties)

        if ring_size is not None:
            self.ring_size = ring_size
        else:
            self.ring_size = MPCTensor.get_ring_size_from_secret(secret, shares)
        self.mpc_shape = shape

        # TODO: We can get this from the the secret if the secret is local
        # TODO: https://app.clubhouse.io/openmined/story/1128/tech-debt-for-adp-smpc-demo?stories_sort_by\
        #  =priority&stories_group_by=WORKFLOW_STATE
        if shape is None:
            raise ValueError("Shape of the secret should be known")

        if secret is not None:
            shares = MPCTensor._get_shares_from_secret(
                secret=secret,
                parties=parties,
                parties_info=self.parties_info,
                shape=shape,
                seed_przs=seed_przs,
                ring_size=self.ring_size,
            )

        if shares is None:
            raise ValueError("Shares should not be None at this step")

        res = list(MPCTensor._mpc_from_shares(shares, parties=parties))

        # we need to make sure that when we zip up clients from
        # multiple MPC tensors that they are in corresponding order
        # so we always sort all of them by the id of the domain
        # TODO: store children as lists of dictionaries because eventually
        # it's likely that we have multiple shares from the same client
        # (For example, if you wanted a domain to have 90% share ownership
        # you'd need to produce 10 shares and give 9 of them to the same domain)
        # TODO captured: https://app.clubhouse.io/openmined/story/1128/tech-debt-for-adp-smpc-\
        #  demo?stories_sort_by=priority&stories_group_by=WORKFLOW_STATE
        res.sort(key=lambda share: share.client.name + share.client.id.no_dash)

        super().__init__(res)

    @staticmethod
    def get_ring_size_from_secret(
        secret: Optional[Any] = None, shares: Optional[List[Any]] = None
    ) -> int:
        if secret is None:
            value = shares[0]  # type: ignore
        else:
            value = secret
        if utils.ispointer(value):
            dtype = getattr(value, "public_dtype", None)
        else:
            dtype = getattr(value, "dtype", None)

        ring_size = utils.TYPE_TO_RING_SIZE.get(dtype, None)
        if ring_size is not None:
            return ring_size

        logger.warning("Ring size was not found! Defaulting to 2**32.")
        return 2**32

    @staticmethod
    def get_parties_info(parties: Iterable[Any]) -> List[GridURL]:
        # relative
        from ....grid.client import GridHTTPConnection

        parties_info: List[GridURL] = []
        for party in parties:
            connection = party.routes[0].connection
            if not isinstance(connection, GridHTTPConnection):
                raise TypeError(
                    f"You tried to pass {type(connection)} for multiplication dependent operation."
                    + "Currently Syft works only with hagrid"
                    + "We apologize for the inconvenience"
                    + "We will add support for local python objects very soon."
                )
            base_url = PARTIES_REGISTER_CACHE.get(party, None)

            if base_url is None:
                base_url = connection.base_url
                PARTIES_REGISTER_CACHE[party] = base_url
                try:
                    pass
                    # We do not use sy.register, should reenable after fixing.
                    # sy.register(  # nosec
                    #     name="Howard Wolowtiz",
                    #     email="howard@mit.edu",
                    #     password="astronaut",
                    #     url=url,
                    #     port=port,
                    #     verbose=False,
                    # )
                except Exception:
                    """ """
                    # TODO : should modify to return same client if registered.
                    # print("Proxy Client already User Register", e)
            if base_url is not None:
                parties_info.append(base_url)
            else:
                raise Exception(
                    f"Failed to get GridURL from {base_url} for party {party}."
                )

        return parties_info

    def publish(self, sigma: float) -> MPCTensor:
        new_shares = []

        for share in self.child:
            share.block

        for share in self.child:
            new_share = share.publish(sigma=sigma)
            new_shares.append(new_share)

        return MPCTensor(
            parties=self.parties,
            shares=new_shares,
            shape=self.mpc_shape,
            seed_przs=self.seed_przs,
        )

    @property
    def shape(self) -> Optional[Tuple[int, ...]]:
        return self.mpc_shape

    @staticmethod
    def _mpc_from_shares(
        shares: List[ShareTensor],
        parties: Optional[List[Any]] = None,
    ) -> List[ShareTensor]:
        if not isinstance(shares, (list, tuple)):
            raise ValueError("_mpc_from_shares expected a list or tuple of shares")

        if utils.ispointer(shares[0]):
            # Remote shares
            return shares
        elif parties is None:
            raise ValueError(
                "Parties should not be None if shares are not already sent to parties"
            )
        else:
            return MPCTensor._mpc_from_local_shares(shares, parties)

    @staticmethod
    def _mpc_from_local_shares(
        shares: List[ShareTensor], parties: List[Any]
    ) -> List[ShareTensor]:
        # TODO: ShareTensor needs to have serde serializer/deserializer
        shares_ptr = [share.send(party) for share, party in zip(shares, parties)]
        return shares_ptr

    @staticmethod
    def _get_shares_from_secret(
        secret: Any,
        parties: List[Any],
        shape: Tuple[int, ...],
        seed_przs: int,
        parties_info: List[GridURL],
        ring_size: int,
    ) -> List[ShareTensor]:
        if utils.ispointer(secret):
            if shape is None:
                raise ValueError("Shape must be specified when the secret is remote")
            return MPCTensor._get_shares_from_remote_secret(
                secret=secret,
                shape=shape,
                parties=parties,
                seed_przs=seed_przs,
                parties_info=parties_info,
                ring_size=ring_size,
            )

        return MPCTensor._get_shares_from_local_secret(
            secret=secret,
            seed_przs=seed_przs,
            ring_size=ring_size,
            shape=shape,
            parties_info=parties_info,
        )

    @staticmethod
    def _get_shares_from_remote_secret(
        secret: Any,
        shape: Tuple[int, ...],
        parties: List[Any],
        seed_przs: int,
        parties_info: List[GridURL],
        ring_size: int,
    ) -> List[ShareTensor]:
        shares = []
        for i, party in enumerate(parties):
            if secret is not None and party == secret.client:
                value = secret
            else:
                value = None

            # relative
            from ..autodp.single_entity_phi import (
                TensorWrappedSingleEntityPhiTensorPointer,
            )

            if isinstance(secret, TensorWrappedSingleEntityPhiTensorPointer):

                share_wrapper = secret.to_local_object_without_private_data_child()
                share_wrapper_pointer = share_wrapper.send(party)

                remote_share = party.syft.core.tensor.smpc.share_tensor.ShareTensor.generate_przs_on_dp_tensor(
                    rank=i,
                    parties_info=parties_info,
                    value=value,
                    shape=shape,
                    seed_przs=seed_przs,
                    share_wrapper=share_wrapper_pointer,
                    ring_size=ring_size,
                )

            else:
                remote_share = (
                    party.syft.core.tensor.smpc.share_tensor.ShareTensor.generate_przs(
                        rank=i,
                        parties_info=parties_info,
                        value=value,
                        shape=shape,
                        seed_przs=seed_przs,
                        ring_size=ring_size,
                    )
                )

            shares.append(remote_share)

        return shares

    @staticmethod
    def _get_shares_from_local_secret(
        secret: Any,
        shape: Tuple[int, ...],
        seed_przs: int,
        parties_info: List[GridURL],
        ring_size: int = 2**32,
    ) -> List[ShareTensor]:
        shares = []
        nr_parties = len(parties_info)
        for i in range(nr_parties):
            if i == nr_parties - 1:
                value = secret
            else:
                value = None

            local_share = ShareTensor.generate_przs(
                rank=i,
                parties_info=parties_info,
                value=value,
                shape=shape,
                seed_przs=seed_przs,
                init_clients=False,
                ring_size=ring_size,
            )

            shares.append(local_share)

        return shares

    def request(
        self,
        reason: str = "",
        block: bool = False,
        timeout_secs: Optional[int] = None,
        verbose: bool = False,
    ) -> None:
        for child in self.child:
            child.request(
                reason=reason, block=block, timeout_secs=timeout_secs, verbose=verbose
            )

    @property
    def block(self) -> "MPCTensor":
        """Block until all shares have been created."""
        for share in self.child:
            share.block

        return self

    def block_with_timeout(self, secs: int, secs_per_poll: int = 1) -> "MPCTensor":
        """Block until all shares have been created or until timeout expires."""

        for share in self.child:
            share.block_with_timeout(secs=secs, secs_per_poll=secs_per_poll)

        return self

    def reconstruct(self) -> np.ndarray:
        # TODO: It might be that the resulted shares (if we run any computation) might
        # not be available at this point. We need to have this fail well with a nice
        # description as to which node wasn't able to be reconstructued.
        # Captured: https://app.clubhouse.io/openmined/story/1128/tech-debt-for-adp-smpc-demo?\
        # stories_sort_by=priority&stories_group_by=WORKFLOW_STATE

        # for now we need to convert the values coming back to int32
        # sometimes they are floats coming from DP
        def convert_child_numpy_type(tensor: Any, np_type: type) -> Any:
            if isinstance(tensor, np.ndarray):
                return np.array(tensor, np_type)
            if hasattr(tensor, "child"):
                tensor.child = convert_child_numpy_type(
                    tensor=tensor.child, np_type=np_type
                )
            return tensor

        dtype = utils.RING_SIZE_TO_TYPE.get(self.ring_size, None)

        if dtype is None:
            raise ValueError(f"Type for ring size {self.ring_size} was not found!")

        for share in self.child:
            if not share.exists:
                raise Exception(
                    "One of the shares doesn't exist. This probably means the SMPC "
                    "computation isn't yet complete. Try again in a moment or call .block.reconstruct()"
                    "instead to block until the SMPC operation is complete which creates this variable."
                )

        local_shares = []
        for share in self.child:
            res = share.get()
            res = convert_child_numpy_type(res, dtype)
            local_shares.append(res)

        is_share_tensor = isinstance(local_shares[0], ShareTensor)

        if is_share_tensor:
            local_shares = [share.child for share in local_shares]

        result = local_shares[0]
        op = ShareTensor.get_op(self.ring_size, "add")
        for share in local_shares[1:]:
            result = op(result, share)

        if hasattr(result, "child") and isinstance(result.child, ShareTensor):
            return result.child.child

        return result

    get = reconstruct

    @staticmethod
    def hook_method(__self: MPCTensor, method_name: str) -> Callable[..., Any]:
        """Hook a framework method.

        Args:
            method_name (str): method to hook

        Returns:
            A hooked method
        """

        def method_all_shares(
            _self: MPCTensor, *args: List[Any], **kwargs: Dict[Any, Any]
        ) -> Any:

            shares = []

            for share in _self.child:
                method = getattr(share, method_name)
                new_share = method(*args, **kwargs)
                shares.append(new_share)

                # TODO: generalize type after fixed precision
                dummy_res = np.random.randint(
                    _self.mpc_shape[0], size=_self.mpc_shape, dtype=np.int32  # type: ignore
                )
                if method_name not in INPLACE_OPS:
                    dummy_res = getattr(dummy_res, method_name)(*args, **kwargs)
                else:
                    getattr(dummy_res, method_name)(*args, **kwargs)

                new_shape = dummy_res.shape
            res = MPCTensor(parties=_self.parties, shares=shares, shape=new_shape)
            return res

        return functools.partial(method_all_shares, __self)

    def __getattribute__(self, attr_name: str) -> Any:

        if attr_name in METHODS_FORWARD_ALL_SHARES:
            return MPCTensor.hook_method(self, attr_name)

        return object.__getattribute__(self, attr_name)

    @staticmethod
    def reshare(mpc_tensor: MPCTensor, parties: Iterable[Any]) -> MPCTensor:
        """Reshare a given secret to a superset of parties.
        Args:
            mpc_tensor(MPCTensor): input MPCTensor to reshare.
            parties(List[Any]): Input parties List.
        Returns:
            res_mpc(MPCTensor): Reshared MPCTensor.
        Raises:
            ValueError: If the input MPCTensor and input parties are same.

        Note:
        We provide an additional layer of abstraction such that,
        when computation is performed on data belonging to different parties
        The underlying secret are automatically converted into secret shares of their input.

        Assume there are two parties Parties P1,P2

        tensor_1 = data_pointer_1 (party 1 data)
        tensor_2 = data_pointer_2 (party 2 data)

        result -------> tensor_1+ tensor_1 (local computation as the data
        belongs to the same party)

        Interesting scenario is when

        result --------> tensor_1+tensor_2

        Each tensor belongs to two different parties.
        There are automatically secret shared without the user
        knowing that a MPCTensor is being created underneath.
        """
        mpc_parties = set(mpc_tensor.parties)
        parties = set(parties)
        shape = mpc_tensor.shape
        seed_przs = mpc_tensor.seed_przs
        client_map = {share.client: share for share in mpc_tensor.child}

        if mpc_parties == parties:
            raise ValueError(
                "Input parties for resharing are same as the input parties."
            )
        parties_info = MPCTensor.get_parties_info(parties)
        shares = [client_map.get(party) for party in parties]
        for i, party in enumerate(parties):
            shares[
                i
            ] = party.syft.core.tensor.smpc.share_tensor.ShareTensor.generate_przs(
                rank=i,
                parties_info=parties_info,
                value=shares[i],
                shape=shape,
                seed_przs=seed_przs,
                ring_size=mpc_tensor.ring_size,
            )

        res_mpc = MPCTensor(shares=shares, ring_size=mpc_tensor.ring_size, shape=shape, parties=parties)  # type: ignore

        return res_mpc

    @staticmethod
    def sanity_checks(mpc_tensor: MPCTensor, other: Any) -> Tuple[MPCTensor, Any]:
        """Performs sanity checks to share data to whole superset of parites involved.
        Args:
            mpc_tensor(MPCTensor): input MPCTensor to perform sanity check on.
            other (Any): input operand.
        Returns:
            Tuple[MPCTensor,Any]: Rehared Tensor values.
        """
        if utils.ispointer(other):
            parties = mpc_tensor.parties
            client = other.client
            public_shape = other.public_shape
            if public_shape is None:
                # TODO: Should be modified after Trask's Synthetic data PR.
                raise ValueError("The input tensor pointer should have public shape.")
            if client not in parties:
                new_parties = [client]
                new_parties += parties
                mpc_tensor = MPCTensor.reshare(mpc_tensor, new_parties)

            other = MPCTensor(secret=other, parties=new_parties, shape=public_shape)

        elif isinstance(other, MPCTensor):
            p1 = set(mpc_tensor.parties)  # parties in first MPCTensor
            p2 = set(other.parties)  # parties in second MPCTensor.
            if p1 != p2:
                parties_union = p1.union(p2)
                mpc_tensor = (
                    MPCTensor.reshare(mpc_tensor, parties_union)
                    if p1 != parties_union
                    else mpc_tensor
                )
                other = (
                    MPCTensor.reshare(other, parties_union)
                    if p2 != parties_union
                    else other
                )

        return mpc_tensor, other

    def __apply_private_op(
        self, other: MPCTensor, op_str: str, **kwargs: Dict[Any, Any]
    ) -> List[ShareTensor]:
        # relative
        from ..tensor import TensorPointer

        op_method = f"__{op_str}__"
        if op_str in {"add", "sub"}:
            if len(self.child) != len(other.child):
                raise ValueError(
                    "Zipping two different lengths will drop data. "
                    + f"{len(self.child)} vs {len(other.child)}"
                )
            if not isinstance(self.child[0], TensorPointer):
                res_shares = [
                    getattr(a, op_method)(a, b, **kwargs)
                    for a, b in zip(self.child, other.child)
                ]
            else:
                res_shares = []
                attr_path_and_name = f"{self.child[0].path_and_name}.__{op_str}__"
                op = get_run_class_method(attr_path_and_name, SMPC=True)
                for x, y in zip(self.child, other.child):
                    res_shares.append(op(x, x, y, **kwargs))

        else:
            raise ValueError(f"MPCTensor Private {op_str} not supported")
        return res_shares

    def __apply_public_op(
        self, y: Any, op_str: str, **kwargs: Dict[Any, Any]
    ) -> List[ShareTensor]:
        # relative
        from ..tensor import TensorPointer

        op_method = f"__{op_str}__"
        if op_str in {"mul", "matmul", "add", "sub"}:
            if not isinstance(self.child[0], TensorPointer):
                res_shares = [
                    getattr(share, op_method)(share, y, **kwargs)
                    for share in self.child
                ]
            else:
                res_shares = []
                attr_path_and_name = f"{self.child[0].path_and_name}.__{op_str}__"
                op = get_run_class_method(attr_path_and_name, SMPC=True)
                for share in self.child:
                    res_shares.append(op(share, share, y, **kwargs))

        else:
            raise ValueError(f"MPCTensor Public {op_str} not supported")

        return res_shares

    def __apply_op(
        self,
        y: Union[int, float, torch.Tensor, np.ndarray, MPCTensor],
        op_str: str,
    ) -> MPCTensor:
        """Apply an operation on "self" which is a MPCTensor "y".

         This function checks if "y" is private or public value.

        Args:
            y (Union[int, float, torch.Tensor, np.ndarray, MPCTensor]: tensor to apply the operation.
            op_str (str): the operation.

        Returns:
            MPCTensor. the operation "op_str" applied on "self" and "y"
        """

        x, y = MPCTensor.sanity_checks(self, y)
        kwargs: Dict[Any, Any] = {"seed_id_locations": secrets.randbits(64)}
        if isinstance(y, MPCTensor):
            result = x.__apply_private_op(y, op_str, **kwargs)
            y_ring_size = y.ring_size
        else:
            result = x.__apply_public_op(y, op_str, **kwargs)
            y_ring_size = MPCTensor.get_ring_size_from_secret(y)

        if self.ring_size != y_ring_size:
            raise ValueError(
                f"Ring size mismatch between self {self.ring_size} and other {y_ring_size}"
            )

        y_shape = getattr(y, "shape", (1,))

        shape = utils.get_shape(op_str, self.shape, y_shape)

        ring_size = utils.get_ring_size(self.ring_size, y_ring_size)

        result = MPCTensor(
            shares=result, shape=shape, ring_size=ring_size, parties=x.parties
        )

        return result

    def add(
        self, y: Union[int, float, np.ndarray, torch.tensor, MPCTensor]
    ) -> MPCTensor:
        """Apply the "add" operation between "self" and "y".

        Args:
            y (Union[MPCTensor, torch.Tensor, float, int]): self + y

        Returns:
            MPCTensor. Result of the operation.
        """
        res = self.__apply_op(y, "add")
        return res

    def sub(self, y: MPCTensor) -> MPCTensor:
        res = self.__apply_op(y, "sub")
        return res

    def rsub(self, y: MPCTensor) -> MPCTensor:
        new_self = self * (-1)
        res = new_self.__apply_op(y, "add")
        return res

    def mul(
        self, y: Union[int, float, np.ndarray, torch.tensor, MPCTensor]
    ) -> MPCTensor:
        # relative
        from ..tensor import TensorPointer

        self, y = MPCTensor.sanity_checks(self, y)
        kwargs: Dict[Any, Any] = {"seed_id_locations": secrets.randbits(64)}
        op = "__mul__"
        res_shares: List[Any] = []
        if isinstance(y, MPCTensor):
            res_shares = spdz.mul_master(self, y, "mul", **kwargs)
        else:
            if not isinstance(self.child[0], TensorPointer):
                res_shares = [
                    getattr(a, op)(a, b, **kwargs) for a, b in zip(self.child, itertools.repeat(y))  # type: ignore
                ]
            else:

                attr_path_and_name = f"{self.child[0].path_and_name}.__mul__"
                tensor_op = get_run_class_method(attr_path_and_name, SMPC=True)
                for share in self.child:
                    res_shares.append(tensor_op(share, share, y, **kwargs))

        y_shape = getattr(y, "shape", (1,))
        new_shape = utils.get_shape("mul", self.mpc_shape, y_shape)
        res = MPCTensor(
            parties=self.parties,
            shares=res_shares,
            shape=new_shape,
            ring_size=self.ring_size,
        )

        return res

    def lt(
        self, y: Union[int, float, np.ndarray, torch.tensor, MPCTensor]
    ) -> MPCTensor:
        mpc_res = spdz.lt_master(self, y, "mul")  # type: ignore

        return mpc_res

    def gt(
        self, y: Union[int, float, np.ndarray, torch.tensor, MPCTensor]
    ) -> MPCTensor:
        mpc_res = MPCTensor.lt(y, self)  # type: ignore

        return mpc_res

    def ge(
        self, y: Union[int, float, np.ndarray, torch.tensor, MPCTensor]
    ) -> MPCTensor:

        mpc_res = 1 - MPCTensor.lt(self, y)

        return mpc_res  # type: ignore

    def le(
        self, y: Union[int, float, np.ndarray, torch.tensor, MPCTensor]
    ) -> MPCTensor:
        mpc_res = 1 - MPCTensor.lt(y, self)  # type: ignore

        return mpc_res  # type: ignore

    def eq(
        self, y: Union[int, float, np.ndarray, torch.tensor, MPCTensor]
    ) -> MPCTensor:
        # TODO: Should make two comparisons parallel
        mpc_res = MPCTensor.le(self, y) - MPCTensor.lt(self, y)

        return mpc_res

    def ne(
        self, y: Union[int, float, np.ndarray, torch.tensor, MPCTensor]
    ) -> MPCTensor:
        mpc_res = 1 - MPCTensor.eq(self, y)

        return mpc_res  # type: ignore

    def matmul(
        self, y: Union[int, float, np.ndarray, torch.tensor, "MPCTensor"]
    ) -> MPCTensor:
        """Apply the "matmul" operation between "self" and "y"
        Args:
            y (Union[int, float, np.ndarray, torch.tensor, "MPCTensor"]): self @ y
        Returns:
            MPCTensor: Result of the opeartion.
        """
        if isinstance(y, ShareTensor):
            raise ValueError("Private matmul not supported yet")

        res = self.__apply_op(y, "matmul")
        return res

    def put(
        self,
        indices: npt.ArrayLike,
        values: npt.ArrayLike,
        mode: Optional[str] = "raise",
    ) -> MPCTensor:
        """Performs Numpy put operation on the underlying ShareTensors.
        Args:
            indices (npt.ArrayLike): Target indices, interpreted as integers.
            values (npt.ArrayLike): Values to place at target indices.
            mode (Optional[str]): Specifies how out-of-bounds indices will behave.
        Returns:
            res (MPCTensor): Result of the operation.
        """
        shares = []
        shares.append(self.child[0].put(indices, values, mode))
        # since the value is public we assign directly to prevent overhead of random share creation.
        zero = np.zeros_like(values)
        for share in self.child[1::]:
            shares.append(share.put(indices, zero.copy(), mode))

        res = MPCTensor(shares=shares, parties=self.parties, shape=self.shape)
        return res

    def sign(self) -> MPCTensor:
        """Calculate sign of given tensor.

        Returns:
            MPCTensor: with computed sign.
        """
        res: MPCTensor = 2 * (self > 0) - 1  # type: ignore
        return res

    def __abs__(self) -> MPCTensor:
        """Calculates absolute value of MPCTensor.

        Returns:
            MPCTensor: computed absolute values of underlying tensor.
        """
        return self * self.sign()

    def __pow__(self, power: int) -> MPCTensor:
        """Compute integer power of a number iteratively using mul.

        - Divide power by 2 and multiply base to itself (if the power is even)
        - Decrement power by 1 to make it even and then follow the first step

        Args:
            power (int): integer value to apply the

        Returns:
             MPCTensor: Result of the pow operation

        Raises:
            RuntimeError: if negative power is given
        """
        # TODO: Implement after we have reciprocal function.
        if power < 0:
            raise RuntimeError("Negative integer powers not supported yet.")

        base = self

        # TODO: should modify for general ring sizes.
        result = np.ones(shape=self.shape, dtype=np.int32)

        while power > 0:
            # If power is odd
            if power % 2 == 1:
                result = base * result
                result.block

            # Divide the power by 2
            power = power // 2
            # Multiply base to itself
            base = base * base
            base.block

        return result

    def __str__(self) -> str:
        res = "MPCTensor"
        for share in self.child:
            res = f"{res}\n\t{share}"

        return res

    @property
    def synthetic(self) -> np.ndarray:
        # TODO finish. max_vals and min_vals not available at present.
        return (
            np.random.rand(*list(self.shape)) * (self.max_vals - self.min_vals)  # type: ignore
            + self.min_vals
        ).astype(self.public_dtype)

    def __repr__(self) -> str:

        # out = self.synthetic.__repr__()
        # out += "\n\n (The data printed above is synthetic - it's an imitation of the real data.)"
        out = ""
        out += "\n\nMPCTensor"
        out += ".shape=" + str(self.shape) + "\n"
        for i, child in enumerate(self.child):
            out += f"\t .child[{i}] = " + child.__repr__() + "\n"
        out = out[:-1] + ""

        return out

    __add__ = add
    __radd__ = add
    __sub__ = sub
    __rsub__ = rsub
    __mul__ = mul
    __rmul__ = mul
    __matmul__ = matmul
    __lt__ = lt
    __gt__ = gt
    __ge__ = ge
    __le__ = le
    __eq__ = eq
    __ne__ = ne


@implements(MPCTensor, np.add)
def add(x: np.ndarray, y: MPCTensor) -> SupportedChainType:
    return y.add(x)


@implements(MPCTensor, np.subtract)
def sub(x: np.ndarray, y: MPCTensor) -> SupportedChainType:
    return y.rsub(x)


@implements(MPCTensor, np.multiply)
def mul(x: np.ndarray, y: MPCTensor) -> SupportedChainType:
    return y.mul(x)


# @implements(MPCTensor, np.greater)
# def mul(x: np.ndarray, y: MPCTensor) -> SupportedChainType:
#     return y.gt(x)
