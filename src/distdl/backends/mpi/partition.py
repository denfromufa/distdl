import numpy as np
from mpi4py import MPI

from distdl.backends.mpi.compare import check_identical_comm
from distdl.backends.mpi.compare import check_identical_group
from distdl.backends.mpi.compare import check_null_comm
from distdl.backends.mpi.compare import check_null_group
from distdl.backends.mpi.compare import check_null_rank
from distdl.utilities.debug import print_sequential
from distdl.utilities.index_tricks import cartesian_index_c
from distdl.utilities.index_tricks import cartesian_index_f


class MPIPartition:

    def __init__(self, comm=MPI.COMM_NULL, group=MPI.GROUP_NULL, root=None):

        self.comm = comm

        # root tracks a root communicator: any subpartition from this one
        # will have the same root as this one.
        if root is None:
            self.root = comm
        else:
            self.root = root

        if self.comm != MPI.COMM_NULL:
            self.active = True
            if group == MPI.GROUP_NULL:
                self.group = comm.Get_group()
            else:
                self.group = group
            self.rank = self.comm.Get_rank()
            self.size = self.comm.Get_size()
        else:
            self.active = False
            self.group = group
            self.rank = MPI.PROC_NULL
            self.size = -1

        self.shape = [1]
        self.index = self.rank

    def __eq__(self, other):

        # MPI_COMM_NULL is not a valid argument to MPI_Comm_compare, per the
        # MPI spec.  Because reasons.
        # We will require two partitions to have MPI_IDENT communicators to
        # consider them to be equal.
        if (check_null_comm(self.comm) or
            check_null_comm(other.comm) or
            check_null_group(self.group) or
            check_null_group(other.group) or
            check_null_rank(self.rank) or
            check_null_rank(other.rank)): # noqa E129
            return False

        return (check_identical_comm(self.comm, other.comm) and
                check_identical_group(self.group, other.group) and
                self.rank == other.rank)

    def print_sequential(self, val):

        if self.active:
            print_sequential(self.comm, val)

    def create_partition_inclusive(self, ranks):

        ranks = np.asarray(ranks)
        group = self.group.Incl(ranks)

        comm = self.comm.Create_group(group)

        return MPIPartition(comm, group, root=self.root)

    def create_partition_union(self, other):

        # Cannot make a union if the two partitions do not share a root
        if not check_identical_comm(self.root, other.root):
            raise Exception()

        group = MPI.Group.Union(self.group, other.group)

        comm = self.root.Create_group(group)

        return MPIPartition(comm, group, root=self.root)

    def create_cartesian_topology_partition(self, shape, **options):

        shape = np.asarray(shape)
        if self.active:
            comm = self.comm.Create_cart(shape, **options)
            group = comm.Get_group()

            if not check_identical_group(self.group, group):
                raise Exception()

            # group = self.group
            return MPICartesianPartition(comm, group, self.root, shape)

        else:
            comm = MPI.COMM_NULL
            return MPIPartition(comm, self.group, root=self.root)

    # P: Partition containing root index (cartesian)
    # P_union: Partition that all ranks are a member of
    # root_index: (cartesian) index of the root of the communication.
    #             This is either the current index if it is the send
    #             group, or it is the index the current index receives
    #             from if this is the receive group.
    # src_indices: All cartesian indices in the entire source partition.
    # dest_indices: All cartesian indices in the entire destination partition
    def _build_cross_partition_groups(self, P, P_union,
                                      root_index, src_indices, dest_indices):

        root_rank = MPI.PROC_NULL
        if P.active:
            # The ranks in the union that I will send data to (broadcast) or
            # receive data from (reduction), if this is the "send" group.
            # The ranks ranks in the union that will receive data from the
            # same place as me (broadcast) or send data to the same place as
            # me (reduction).
            dest_ranks = np.where(dest_indices == root_index)[0]
            # My rank in the union (send group for broadcast or receive group
            # for reduction) or the rank in the union I will receive data from
            # (recv group for broadcast) or send data to (send group for
            # reduction).
            root_rank = np.where(src_indices == root_index)[0][0]

        # Create the MPI group
        ranks = []
        group = MPI.GROUP_NULL
        if root_rank != MPI.PROC_NULL:
            # Ensure that the root rank is first, so it will be rank 0 in the
            # new communicator and that ranks are not repeated in the union
            ranks = [root_rank] + [rank for rank in dest_ranks if rank != root_rank]
            ranks = np.array(ranks)
            group = P_union.group.Incl(ranks)

        return ranks, group

    def _create_send_recv_partitions(self, P_union,
                                     send_ranks, group_send,
                                     recv_ranks, group_recv):
        # We will only do certain work if certain groups were created.
        has_send_group = not check_null_group(group_send)
        has_recv_group = not check_null_group(group_recv)
        same_send_recv_group = check_identical_group(group_send, group_recv)

        P_send = MPIPartition()
        P_recv = MPIPartition()

        # Brute force the four cases, don't try to be elegant...
        if has_send_group and has_recv_group and not same_send_recv_group:

            # If we have to both send and receive, it is possible to deadlock
            # if we try to create all send groups first.  Instead, we have to
            # create them starting from whichever has the smallest root rank,
            # first.  This way, we should be able to guarantee that deadlock
            # cannot happen.  It may be linear time, but this is part of the
            # setup phase anyway.
            if recv_ranks[0] < send_ranks[0]:
                comm_recv = P_union.comm.Create_group(group_recv, tag=recv_ranks[0])
                P_recv = MPIPartition(comm_recv, group_recv,
                                      root=P_union.root)
                comm_send = P_union.comm.Create_group(group_send, tag=send_ranks[0])
                P_send = MPIPartition(comm_send, group_send,
                                      root=P_union.root)
            else:
                comm_send = P_union.comm.Create_group(group_send, tag=send_ranks[0])
                P_send = MPIPartition(comm_send, group_send,
                                      root=P_union.root)
                comm_recv = P_union.comm.Create_group(group_recv, tag=recv_ranks[0])
                P_recv = MPIPartition(comm_recv, group_recv,
                                      root=P_union.root)
        elif has_send_group and not has_recv_group and not same_send_recv_group:
            comm_send = P_union.comm.Create_group(group_send, tag=send_ranks[0])
            P_send = MPIPartition(comm_send, group_send,
                                  root=P_union.root)
        elif not has_send_group and has_recv_group and not same_send_recv_group:
            comm_recv = P_union.comm.Create_group(group_recv, tag=recv_ranks[0])
            P_recv = MPIPartition(comm_recv, group_recv,
                                  root=P_union.root)
        else:  # if has_send_group and has_recv_group and same_send_recv_group
            comm_send = P_union.comm.Create_group(group_send, tag=send_ranks[0])
            P_send = MPIPartition(comm_send, group_send,
                                  root=P_union.root)
            P_recv = P_send

        return P_send, P_recv

    def create_broadcast_partition_to(self, P_dest,
                                      transpose_src=False,
                                      transpose_dest=False):

        P_src = self

        P_send = MPIPartition()
        P_recv = MPIPartition()

        P_union = MPIPartition()
        if P_src.active or P_dest.active:
            P_union = P_src.create_partition_union(P_dest)

        # If we are not active in one of the two partitions, return null
        # partitions
        if not P_union.active:
            return P_send, P_recv

        # Get the rank and shape of the two partitions
        data = None
        if P_src.active:
            data = P_src.shape
        P_src_shape = P_union.broadcast_data(data, P_data=P_src)
        src_dim = len(P_src_shape)

        data = None
        if P_dest.active:
            data = P_dest.shape
        P_dest_shape = P_union.broadcast_data(data, P_data=P_dest)
        dest_dim = len(P_dest_shape)

        # The source must be smaller (or equal) in size to the destination.
        if src_dim > dest_dim:
            raise Exception("No broadcast: Source partition larger than "
                            "destination partition.")

        # Share the src partition dimensions with everyone.  We will compare
        # this with the destination dimensions, so we pad it to the left with
        # ones to make a valid comparison.
        src_shape = np.ones(dest_dim, dtype=np.int)
        src_shape[-src_dim:] = P_src_shape[::-1] if transpose_src else P_src_shape
        dest_shape = P_dest_shape[::-1] if transpose_dest else P_dest_shape

        # Find any location that the dimensions differ and where the source
        # dimension is not 1 where they differ.  If there are any such
        # dimensions, we cannot perform a valid broadcast.
        no_match_loc = np.where((src_shape != dest_shape) & (src_shape != 1))[0]

        if len(no_match_loc) > 0:
            raise Exception("No broadcast: Dimensions don't match or "
                            "source is not 1 where there is a mismatch.")

        # We will use the matching dimensions to compute the broadcast indices
        match_loc = np.where((src_shape == dest_shape))[0]

        # Compute the Cartesian index of the source rank, in the matching
        # dimensions only.  This index will be constant in the dimensions of
        # the destination that we are broadcasting along.
        src_flat_index = -1
        if P_src.active:
            src_cart_index = np.zeros_like(src_shape)
            c = P_src.index
            if transpose_src:
                src_cart_index[-src_dim:] = c[::-1]
                src_flat_index = cartesian_index_f(src_shape[match_loc],
                                                   src_cart_index[match_loc])
            else:
                src_cart_index[-src_dim:] = c
                src_flat_index = cartesian_index_c(src_shape[match_loc],
                                                   src_cart_index[match_loc])
        data = np.array([src_flat_index], dtype=np.int)
        src_flat_indices = P_union.allgather_data(data)

        # Compute the Cartesian index of the destination rank, in the matching
        # dimensions only.  This index will match the index in the source we
        # receive the broadcast from.
        dest_flat_index = -1
        if P_dest.active:
            dest_cart_index = P_dest.index
            if transpose_dest:
                dest_cart_index = dest_cart_index[::-1]
                dest_flat_index = cartesian_index_f(dest_shape[match_loc],
                                                    dest_cart_index[match_loc])
            else:
                dest_flat_index = cartesian_index_c(dest_shape[match_loc],
                                                    dest_cart_index[match_loc])
        data = np.array([dest_flat_index], dtype=np.int)
        dest_flat_indices = P_union.allgather_data(data)

        # Build partitions to communicate single broadcasts across subsets
        # of the union partition.

        # Send ranks are P_union ranks in the send group, the first entry
        # is the root of the group.
        send_ranks, group_send = self._build_cross_partition_groups(P_src,
                                                                    P_union,
                                                                    src_flat_index,
                                                                    src_flat_indices,
                                                                    dest_flat_indices)
        # Recv ranks are P_union ranks in the recv group, the first entry
        # is the root of the group.
        recv_ranks, group_recv = self._build_cross_partition_groups(P_dest,
                                                                    P_union,
                                                                    dest_flat_index,
                                                                    src_flat_indices,
                                                                    dest_flat_indices)

        return self._create_send_recv_partitions(P_union,
                                                 send_ranks, group_send,
                                                 recv_ranks, group_recv)

    def create_reduction_partition_to(self, P_dest,
                                      transpose_src=False,
                                      transpose_dest=False):

        P_src = self

        P_send = MPIPartition()
        P_recv = MPIPartition()

        P_union = MPIPartition()
        if P_src.active or P_dest.active:
            P_union = P_src.create_partition_union(P_dest)

        # If we are not active in one of the two partitions, return null
        # partitions
        if not P_union.active:
            return P_send, P_recv

        # Get the rank and shape of the two partitions
        data = None
        if P_src.active:
            data = P_src.shape
        P_src_shape = P_union.broadcast_data(data, P_data=P_src)
        src_dim = len(P_src_shape)

        data = None
        if P_dest.active:
            data = P_dest.shape
        P_dest_shape = P_union.broadcast_data(data, P_data=P_dest)
        dest_dim = len(P_dest_shape)

        # The source must be smaller (or equal) in size to the destination.
        if dest_dim > src_dim:
            raise Exception("No reduction: Source partition smaller than "
                            "destination partition.")

        src_shape = P_src_shape[::-1] if transpose_src else P_src_shape
        dest_shape = np.ones(src_dim, dtype=np.int)
        dest_shape[-dest_dim:] = P_dest_shape[::-1] if transpose_dest else P_dest_shape

        # Find any location that the dimensions differ and where the dest
        # dimension is not 1 where they differ.  If there are any such
        # dimensions, we cannot perform a valid reduction.
        no_match_loc = np.where((src_shape != dest_shape) & (dest_shape != 1))[0]

        if len(no_match_loc) > 0:
            raise Exception("No broadcast: Dimensions don't match or "
                            "source is not 1 where there is a mismatch.")

        # We will use the matching dimensions to compute the broadcast indices
        match_loc = np.where((src_shape == dest_shape))[0]

        # Compute the Cartesian index of the source rank, in the matching
        # dimensions only.  This index will be constant in the dimensions of
        # the destination that we are reducing along.
        src_flat_index = -1
        if P_src.active:
            src_cart_index = P_src.index
            if transpose_src:
                src_cart_index = src_cart_index[::-1]
                src_flat_index = cartesian_index_f(src_shape[match_loc],
                                                   src_cart_index[match_loc])
            else:
                src_flat_index = cartesian_index_c(src_shape[match_loc],
                                                   src_cart_index[match_loc])
        data = np.array([src_flat_index], dtype=np.int)
        src_flat_indices = P_union.allgather_data(data)

        # Compute the Cartesian index of the destination rank, in the matching
        # dimensions only.  This index will match the index in the source we
        # receive the broadcast from.
        dest_flat_index = -1
        if P_dest.active:
            dest_cart_index = np.zeros_like(dest_shape)
            c = P_dest.index
            if transpose_dest:
                dest_cart_index[:dest_dim] = c
                dest_cart_index = dest_cart_index[::-1]
                dest_flat_index = cartesian_index_f(dest_shape[match_loc],
                                                    dest_cart_index[match_loc])
            else:
                dest_cart_index[-dest_dim:] = c
                dest_flat_index = cartesian_index_c(dest_shape[match_loc],
                                                    dest_cart_index[match_loc])
        data = np.array([dest_flat_index], dtype=np.int)
        dest_flat_indices = P_union.allgather_data(data)

        # Build partitions to communicate single reductions across subsets
        # of the union partition.

        # Send ranks are P_union ranks in the send group, the first entry
        # is the root of the group.
        send_ranks, group_send = self._build_cross_partition_groups(P_src,
                                                                    P_union,
                                                                    src_flat_index,
                                                                    dest_flat_indices,
                                                                    src_flat_indices)
        # Recv ranks are P_union ranks in the recv group, the first entry
        # is the root of the group.
        recv_ranks, group_recv = self._build_cross_partition_groups(P_dest,
                                                                    P_union,
                                                                    dest_flat_index,
                                                                    dest_flat_indices,
                                                                    src_flat_indices)

        return self._create_send_recv_partitions(P_union,
                                                 send_ranks, group_send,
                                                 recv_ranks, group_recv)

    def broadcast_data(self, data, root=0, P_data=None):

        # If the data is coming from a different partition
        if not self.active:
            return None

        if P_data is None:
            P_data = self
            data_root = root
        else:
            # Find the root rank (on P_data) in the self communicator
            rank_map = -1*np.ones(self.size, dtype=np.int)
            rank_map_data = np.array([-1], dtype=np.int)
            if P_data.active:
                rank_map_data[0] = P_data.rank
            self.comm.Allgather(rank_map_data, rank_map)

            if root in rank_map:
                data_root = np.where(rank_map == root)[0][0]
            else:
                raise ValueError("Requested root rank is not in P_data.")

        # Give everyone the size of the data
        data_dim = np.zeros(1, dtype=np.int)
        if P_data.active and self.rank == data_root:
            # Ensure that data is a numpy array
            data = np.atleast_1d(data)
            data_dim[0] = len(data)
        self.comm.Bcast(data_dim, root=data_root)

        out_data = np.ones(data_dim, dtype=np.int)
        if P_data.active and P_data.rank == root:
            out_data = data

        self.comm.Bcast(out_data, root=data_root)

        return out_data

    def allgather_data(self, data):

        data = np.atleast_1d(data)
        sz = len(data)

        out_data = -1*np.ones(sz*self.size, dtype=np.int)
        self.comm.Allgather(data, out_data)
        out_data.shape = -1, sz

        return out_data


class MPICartesianPartition(MPIPartition):

    def __init__(self, comm, group, root, shape):

        super(MPICartesianPartition, self).__init__(comm, group, root)

        self.shape = np.asarray(shape).astype(np.int)
        self.dim = len(self.shape)

        self.index = None
        if self.active:
            self.index = self.cartesian_index(self.rank)

    def create_cartesian_subtopology_partition(self, remain_shape):

        # remain_shape = np.asarray(remain_shape)
        if self.active:
            comm = self.comm.Sub(remain_shape)
            group = comm.Get_group()

            return MPICartesianPartition(comm, group,
                                         self.root,
                                         self.shape[remain_shape == True]) # noqa E712

        else:
            comm = MPI.COMM_NULL
            return MPIPartition(comm, root=self.root)

    def cartesian_index(self, rank):

        if not self.active:
            raise Exception()

        return np.asarray(self.comm.Get_coords(rank))

    def neighbor_ranks(self, rank):

        if not self.active:
            raise Exception()

        index = self.cartesian_index(rank)

        # Resulting list
        neighbor_ranks = []

        # Loop over the dimensions and add the ranks at the neighboring index to the list
        for i in range(self.dim):
            lindex = [x-1 if j == i else x for j, x in enumerate(index)]
            rindex = [x+1 if j == i else x for j, x in enumerate(index)]
            lrank = MPI.PROC_NULL if -1 == lindex[i] else self.comm.Get_cart_rank(lindex)
            rrank = MPI.PROC_NULL if self.shape[i] == rindex[i] else self.comm.Get_cart_rank(rindex)
            neighbor_ranks.append((lrank, rrank))

        return neighbor_ranks
