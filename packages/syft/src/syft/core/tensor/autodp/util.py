# stdlib
# stdlib
from typing import List

# third party
import ray

# relative
from .... import serialize
from ....proto.core.adp.phi_tensor_pb2 import (
    SingleEntityPhiTensor as SingleEntityPhiTensor_PB,
)
from .single_entity_phi import SingleEntityPhiTensor

ray.init()


@ray.remote
def ray_serialize(
    input_list: List[SingleEntityPhiTensor],
) -> List[SingleEntityPhiTensor_PB]:
    return [serialize(obj) for obj in input_list]
