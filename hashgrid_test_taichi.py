import numpy as np
import taichi as ti
import torch
import torch_optimizer as toptim
from taichi.math import uvec3
from torch.cuda.amp import custom_bwd, custom_fwd
from tqdm import tqdm

from threestudio.models.networks import ProgressiveBandHashGrid
from threestudio.utils.misc import get_rank

taichi_block_size = 128

data_type = ti.f32
torch_type = torch.float32

MAX_SAMPLES = 1024
NEAR_DISTANCE = 0.01
SQRT3 = 1.7320508075688772
SQRT3_MAX_SAMPLES = SQRT3 / 1024
SQRT3_2 = 1.7320508075688772 * 2


@ti.kernel
def torch2ti(field: ti.template(), data: ti.types.ndarray()):
    for I in ti.grouped(data):
        field[I] = data[I]


@ti.kernel
def ti2torch(field: ti.template(), data: ti.types.ndarray()):
    for I in ti.grouped(data):
        data[I] = field[I]


@ti.kernel
def ti2torch_grad(field: ti.template(), grad: ti.types.ndarray()):
    for I in ti.grouped(grad):
        grad[I] = field.grad[I]


@ti.kernel
def torch2ti_grad(field: ti.template(), grad: ti.types.ndarray()):
    for I in ti.grouped(grad):
        field.grad[I] = grad[I]


@ti.kernel
def torch2ti_vec(field: ti.template(), data: ti.types.ndarray()):
    for I in range(data.shape[0] // 2):
        field[I] = ti.Vector([data[I * 2], data[I * 2 + 1]])


@ti.kernel
def ti2torch_vec(field: ti.template(), data: ti.types.ndarray()):
    for i, j in ti.ndrange(data.shape[0], data.shape[1] // 2):
        data[i, j * 2] = field[i, j][0]
        data[i, j * 2 + 1] = field[i, j][1]


@ti.kernel
def ti2torch_grad_vec(field: ti.template(), grad: ti.types.ndarray()):
    for I in range(grad.shape[0] // 2):
        grad[I * 2] = field.grad[I][0]
        grad[I * 2 + 1] = field.grad[I][1]


@ti.kernel
def torch2ti_grad_vec(field: ti.template(), grad: ti.types.ndarray()):
    for i, j in ti.ndrange(grad.shape[0], grad.shape[1] // 2):
        field.grad[i, j][0] = grad[i, j * 2]
        field.grad[i, j][1] = grad[i, j * 2 + 1]


half2 = ti.types.vector(n=2, dtype=ti.f16)


@ti.kernel
def random_initialize(data: ti.types.ndarray()):
    for I in ti.grouped(data):
        data[I] = (ti.random() * 2.0 - 1.0) * 1e-4


@ti.kernel
def ti_copy(data1: ti.template(), data2: ti.template()):
    for I in ti.grouped(data1):
        data1[I] = data2[I]


@ti.kernel
def ti_copy_array(data1: ti.types.ndarray(), data2: ti.types.ndarray()):
    for I in ti.grouped(data1):
        data1[I] = data2[I]


@ti.kernel
def ti_copy_field_array(data1: ti.template(), data2: ti.types.ndarray()):
    for I in ti.grouped(data1):
        data1[I] = data2[I]


@ti.func
def fast_hash(pos_grid_local):
    result = ti.uint32(0)
    # primes = uvec3(ti.uint32(1), ti.uint32(1958374283), ti.uint32(2654435761))
    primes = uvec3(ti.uint32(1), ti.uint32(2654435761), ti.uint32(805459861))
    for i in ti.static(range(3)):
        result ^= ti.uint32(pos_grid_local[i]) * primes[i]
    return result


@ti.func
def under_hash(pos_grid_local, resolution):
    result = ti.uint32(0)
    stride = ti.uint32(1)
    for i in ti.static(range(3)):
        result += ti.uint32(pos_grid_local[i] * stride)
        stride *= resolution
    return result


@ti.func
def grid_pos2hash_index(indicator, pos_grid_local, resolution, map_size):
    hash_result = ti.uint32(0)
    if indicator == 1:
        hash_result = under_hash(pos_grid_local, resolution)
    else:
        hash_result = fast_hash(pos_grid_local)

    return hash_result % map_size


@ti.kernel
def hash_encode_kernel(
    xyzs: ti.template(),
    table: ti.template(),
    xyzs_embedding: ti.template(),
    hash_map_indicator: ti.template(),
    hash_map_sizes_field: ti.template(),
    offsets: ti.template(),
    B: ti.i32,
    per_level_scale: ti.f32,
):
    # get hash table embedding
    ti.loop_config(block_dim=16)
    for i, level in ti.ndrange(B, 16):
        xyz = ti.Vector([xyzs[i, 0], xyzs[i, 1], xyzs[i, 2]])

        scale = 16 * ti.exp(level * ti.log(per_level_scale)) - 1.0
        resolution = ti.cast(ti.ceil(scale), ti.uint32) + 1

        offset = offsets[level] * 2

        pos = xyz * scale + 0.5
        pos_grid_uint = ti.cast(ti.floor(pos), ti.uint32)
        pos -= pos_grid_uint

        indicator = hash_map_indicator[level]
        map_size = hash_map_sizes_field[level]

        local_feature_0 = 0.0
        local_feature_1 = 0.0

        for idx in ti.static(range(8)):
            w = 1.0
            pos_grid_local = uvec3(0)

            for d in ti.static(range(3)):
                if (idx & (1 << d)) == 0:
                    pos_grid_local[d] = pos_grid_uint[d]
                    w *= 1 - pos[d]
                else:
                    pos_grid_local[d] = pos_grid_uint[d] + 1
                    w *= pos[d]

            index = grid_pos2hash_index(indicator, pos_grid_local, resolution, map_size)
            index_table = offset + index * 2
            index_table_int = ti.cast(index_table, ti.int32)
            local_feature_0 += w * table[index_table_int]
            local_feature_1 += w * table[index_table_int + 1]

        xyzs_embedding[i, level * 2] = local_feature_0
        xyzs_embedding[i, level * 2 + 1] = local_feature_1


@ti.kernel
def hash_encode_kernel_half2(
    xyzs: ti.template(),
    table: ti.template(),
    xyzs_embedding: ti.template(),
    hash_map_indicator: ti.template(),
    hash_map_sizes_field: ti.template(),
    offsets: ti.template(),
    B: ti.i32,
    per_level_scale: ti.f16,
):
    # get hash table embedding
    ti.loop_config(block_dim=32)
    for i, level in ti.ndrange(B, 16):
        xyz = ti.Vector([xyzs[i, 0], xyzs[i, 1], xyzs[i, 2]])

        scale = 16 * ti.exp(level * ti.log(per_level_scale)) - 1.0
        resolution = ti.cast(ti.ceil(scale), ti.uint32) + 1

        offset = offsets[level]

        pos = xyz * scale + 0.5
        pos_grid_uint = ti.cast(ti.floor(pos), ti.uint32)
        pos -= pos_grid_uint

        indicator = hash_map_indicator[level]
        map_size = hash_map_sizes_field[level]

        local_feature = half2(0.0)
        for idx in ti.static(range(8)):
            w = ti.f32(1.0)
            pos_grid_local = uvec3(0)

            for d in ti.static(range(3)):
                if (idx & (1 << d)) == 0:
                    pos_grid_local[d] = pos_grid_uint[d]
                    w *= 1 - pos[d]
                else:
                    pos_grid_local[d] = pos_grid_uint[d] + 1
                    w *= pos[d]

            index = grid_pos2hash_index(indicator, pos_grid_local, resolution, map_size)

            index_table = offset + index
            index_table_int = ti.cast(index_table, ti.int32)

            local_feature += w * table[index_table_int]
        xyzs_embedding[i, level] = local_feature


class HashEncoderTaichi(torch.nn.Module):
    def __init__(
        self,
        b=1.3195079565048218,
        batch_size=8192,
        data_type=data_type,
        half2_opt=False,
    ):
        super(HashEncoderTaichi, self).__init__()

        self.per_level_scale = b
        if batch_size < 2048:
            batch_size = 2048

        # per_level_scale = 1.3195079565048218
        print("per_level_scale: ", b)
        self.offsets = ti.field(ti.i32, shape=(16,))
        self.hash_map_sizes_field = ti.field(ti.uint32, shape=(16,))
        self.hash_map_indicator = ti.field(ti.i32, shape=(16,))
        base_res = 16
        max_params = 2**19
        offset_ = 0
        hash_map_sizes = []
        for i in range(16):
            resolution = (
                int(np.ceil(base_res * np.exp(i * np.log(self.per_level_scale)) - 1.0))
                + 1
            )
            params_in_level = resolution**3
            params_in_level = (
                int(resolution**3)
                if params_in_level % 8 == 0
                else int((params_in_level + 8 - 1) / 8) * 8
            )
            params_in_level = min(max_params, params_in_level)
            self.offsets[i] = offset_
            hash_map_sizes.append(params_in_level)
            self.hash_map_indicator[i] = 1 if resolution**3 <= params_in_level else 0
            offset_ += params_in_level
        print("offset_: ", offset_)
        size = np.uint32(np.array(hash_map_sizes))
        self.hash_map_sizes_field.from_numpy(size)

        self.total_hash_size = offset_ * 2
        print("total_hash_size: ", self.total_hash_size)

        self.hash_table = torch.nn.Parameter(
            torch.zeros(self.total_hash_size, dtype=torch_type), requires_grad=True
        )
        random_initialize(self.hash_table)

        if half2_opt:
            assert self.total_hash_size % 2 == 0
            self.parameter_fields = half2.field(
                shape=(self.total_hash_size // 2,), needs_grad=True
            )
            self.output_fields = half2.field(
                shape=(batch_size * 1024, 16), needs_grad=True
            )

            self.torch2ti = torch2ti_vec
            self.ti2torch = ti2torch_vec
            self.ti2torch_grad = ti2torch_grad_vec
            self.torch2ti_grad = torch2ti_grad_vec

            self._hash_encode_kernel = hash_encode_kernel_half2
        else:
            self.parameter_fields = ti.field(
                data_type, shape=(self.total_hash_size,), needs_grad=True
            )
            self.output_fields = ti.field(
                dtype=data_type, shape=(batch_size * 1024, 32), needs_grad=True
            )
            self.torch2ti = torch2ti
            self.ti2torch = ti2torch
            self.ti2torch_grad = ti2torch_grad
            self.torch2ti_grad = torch2ti_grad

            self._hash_encode_kernel = hash_encode_kernel

        self.input_fields = ti.field(
            dtype=data_type, shape=(batch_size * 1024, 3), needs_grad=True
        )
        self.output_dim = 32  # the output dim: num levels (16) x level num (2)
        self.register_buffer(
            "hash_grad", torch.zeros(self.total_hash_size, dtype=torch_type)
        )
        self.register_buffer(
            "output_embedding", torch.zeros(batch_size * 1024, 32, dtype=torch_type)
        )

        class _module_function(torch.autograd.Function):
            @staticmethod
            @custom_fwd(cast_inputs=torch_type)
            def forward(ctx, input_pos, params):
                output_embedding = self.output_embedding[
                    : input_pos.shape[0]
                ].contiguous()
                torch2ti(self.input_fields, input_pos.contiguous())
                self.torch2ti(self.parameter_fields, params.contiguous())

                self._hash_encode_kernel(
                    self.input_fields,
                    self.parameter_fields,
                    self.output_fields,
                    self.hash_map_indicator,
                    self.hash_map_sizes_field,
                    self.offsets,
                    input_pos.shape[0],
                    self.per_level_scale,
                )
                self.ti2torch(self.output_fields, output_embedding)

                return output_embedding

            @staticmethod
            @custom_bwd
            def backward(ctx, doutput):
                self.zero_grad()

                self.torch2ti_grad(self.output_fields, doutput.contiguous())
                self._hash_encode_kernel.grad(
                    self.input_fields,
                    self.parameter_fields,
                    self.output_fields,
                    self.hash_map_indicator,
                    self.hash_map_sizes_field,
                    self.offsets,
                    doutput.shape[0],
                    self.per_level_scale,
                )
                self.ti2torch_grad(self.parameter_fields, self.hash_grad.contiguous())
                return None, self.hash_grad

        self._module_function = _module_function

    def zero_grad(self):
        self.parameter_fields.grad.fill(0.0)

    def forward(self, positions, bound=1):
        positions = (positions + bound) / (2 * bound)
        return self._module_function.apply(positions, self.hash_table)


taichi_half2_opt = True
taichi_init_args = {"arch": ti.cuda, "device_memory_GB": 4.0}
if taichi_half2_opt:
    taichi_init_args["half2_vectorization"] = True
ti.init(**taichi_init_args)


in_channels = 3
pos_encoding_config = {
    "otype": "ProgressiveBandHashGrid",
    "n_levels": 16,
    "n_features_per_level": 2,
    "log2_hashmap_size": 19,
    "base_resolution": 16,
    "per_level_scale": 1.447269237440378,
    "start_level": 8,
    "start_step": 2000,
    "update_steps": 500,
}
# encoding = ProgressiveBandHashGrid(
#     in_channels, pos_encoding_config, dtype=torch.float32
# )

encoding = HashEncoderTaichi(b=1.447269237440378)
# optim = torch.optim.Adam(encoding.parameters(), lr=1e-3)
# optim = toptim.Adahessian(encoding.parameters(), lr=1e-2, hessian_power=1.0)
optim = torch.optim.LBFGS(encoding.parameters(), lr=1)

with torch.no_grad():
    xy_eval = torch.randn(2, in_channels).cuda()
    print(encoding(xy_eval))

for i in tqdm(range(100)):

    def closure():
        xy = torch.randn(16, in_channels).cuda()
        optim.zero_grad()
        xy = encoding(xy)
        loss = (xy - 1.0).abs().mean()
        loss.backward()
        return loss

    optim.step(closure=closure)

with torch.no_grad():
    xy_eval = torch.randn(2, in_channels).cuda()
    print(encoding(xy_eval))
