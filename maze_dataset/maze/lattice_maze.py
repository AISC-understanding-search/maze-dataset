import typing
import warnings
from dataclasses import dataclass
from itertools import chain
from typing import cast

import numpy as np
from jaxtyping import Bool, Int, Shaped
from muutils.json_serialize.serializable_dataclass import (
    SerializableDataclass,
    serializable_dataclass,
    serializable_field,
)
from muutils.misc import list_split
from muutils.tensor_utils import NDArray

from maze_dataset.constants import (
    NEIGHBORS_MASK,
    SPECIAL_TOKENS,
    Coord,
    CoordArray,
    CoordTup,
)
from maze_dataset.tokenization.token_utils import (
    get_adj_list_tokens,
    get_path_tokens,
    tokens_to_coords,
)

ConnectionList = Bool[np.ndarray, "lattice_dim x y"]
RGB = tuple[int, int, int]

PixelGrid = Int[np.ndarray, "x y rgb"]
BinaryPixelGrid = Bool[np.ndarray, "x y"]


def _fill_edges_with_walls(connection_list: ConnectionList) -> ConnectionList:
    """fill the last elements of the connections lists as false for each dim"""
    for dim in range(connection_list.shape[0]):
        # last row for down
        if dim == 0:
            connection_list[dim, -1, :] = False
        # last column for right
        elif dim == 1:
            connection_list[dim, :, -1] = False
        else:
            raise NotImplementedError(f"only 2d lattices supported. got {dim=}")
    return connection_list


def color_in_pixel_grid(pixel_grid: PixelGrid, color: RGB) -> bool:
    for row in pixel_grid:
        for pixel in row:
            if np.all(pixel == color):
                return True
    return False


@dataclass(frozen=True)
class PixelColors:
    WALL: RGB = (0, 0, 0)
    OPEN: RGB = (255, 255, 255)
    START: RGB = (0, 255, 0)
    END: RGB = (255, 0, 0)
    PATH: RGB = (0, 0, 255)


@dataclass(frozen=True)
class AsciiChars:
    WALL: str = "#"
    OPEN: str = " "
    START: str = "S"
    END: str = "E"
    PATH: str = "X"


ASCII_PIXEL_PAIRINGS: dict[str, RGB] = {
    AsciiChars.WALL: PixelColors.WALL,
    AsciiChars.OPEN: PixelColors.OPEN,
    AsciiChars.START: PixelColors.START,
    AsciiChars.END: PixelColors.END,
    AsciiChars.PATH: PixelColors.PATH,
}


def str_is_coord(coord_str: str) -> bool:
    """return True if the string is a coordinate string, False otherwise"""
    return all(
        [
            coord_str.startswith("("),
            coord_str.endswith(")"),
            "," in coord_str,
            all([x.isdigit() for x in coord_str.lstrip("(").rstrip(")").split(",")]),
        ]
    )


def coord_str_to_tuple(coord_str: str) -> CoordTup:
    """convert a coordinate string to a tuple"""

    stripped: str = coord_str.lstrip("(").rstrip(")")
    return tuple(int(x) for x in stripped.split(","))


def coord_str_to_tuple_noneable(coord_str: str) -> CoordTup | None:
    """convert a coordinate string to a tuple, or None if the string is not a coordinate string"""
    if not str_is_coord(coord_str):
        return None
    return coord_str_to_tuple(coord_str)


def coord_to_str(coord: typing.Sequence[int]) -> str:
    """convert a coordinate to a string"""
    return f"({','.join(str(c) for c in coord)})"


@serializable_dataclass(
    frozen=True,
    kw_only=True,
    properties_to_serialize=["lattice_dim", "generation_meta"],
)
class LatticeMaze(SerializableDataclass):
    """lattice maze (nodes on a lattice, connections only to neighboring nodes)

    Connection List represents which nodes (N) are connected in each direction.

    First and second elements represent rightward and downward connections,
    respectively.

    Example:
      Connection list:
        [
          [ # down
            [F T],
            [F F]
          ],
          [ # right
            [T F],
            [T F]
          ]
        ]

      Nodes with connections
        N T N F
        F   T
        N T N F
        F   F

      Graph:
        N - N
            |
        N - N

    Note: the bottom row connections going down, and the
    right-hand connections going right, will always be False.
    """

    connection_list: ConnectionList
    generation_meta: dict | None = serializable_field(default=None, compare=False)

    lattice_dim = property(lambda self: self.connection_list.shape[0])
    grid_shape = property(lambda self: self.connection_list.shape[1:])
    n_connections = property(lambda self: self.connection_list.sum())

    # ============================================================
    # basic methods
    # ============================================================
    @staticmethod
    def heuristic(a: CoordTup, b: CoordTup) -> float:
        """return manhattan distance between two points"""
        return np.abs(a[0] - b[0]) + np.abs(a[1] - b[1])

    def __hash__(self) -> int:
        return hash(self.connection_list.tobytes())

    def nodes_connected(self, a: Coord, b: Coord, /) -> bool:
        """returns whether two nodes are connected"""
        delta: Coord = b - a
        if np.abs(delta).sum() != 1:
            # return false if not even adjacent
            return False
        else:
            # test for wall
            dim: int = np.argmax(np.abs(delta))
            clist_node: Coord = a if (delta.sum() > 0) else b
            return self.connection_list[dim, clist_node[0], clist_node[1]]

    def get_coord_neighbors(self, c: Coord) -> CoordArray:
        neighbors: list[Coord] = [
            neighbor
            for neighbor in (c + NEIGHBORS_MASK)
            if (
                (0 <= neighbor[0] < self.grid_shape[0])  # in x bounds
                and (0 <= neighbor[1] < self.grid_shape[1])  # in y bounds
                and self.nodes_connected(c, neighbor)  # connected
            )
        ]

        output: CoordArray = np.array(neighbors)
        if len(neighbors) > 0:
            assert output.shape == (
                len(neighbors),
                2,
            ), f"invalid shape: {output.shape}, expected ({len(neighbors)}, 2))\n{c = }\n{neighbors = }\n{self.as_ascii()}"
        return output

    def gen_connected_component_from(self, c: Coord) -> CoordArray:
        """return the connected component from a given coordinate"""
        # Stack for DFS
        stack: list[Coord] = [c]

        # Set to store visited nodes
        visited: set[CoordTup] = set()

        while stack:
            current_node = stack.pop()
            visited.add(tuple(current_node.tolist()))

            # Get the neighbors of the current node
            neighbors = self.get_coord_neighbors(current_node)

            # Iterate over neighbors
            for neighbor in neighbors:
                if tuple(neighbor.tolist()) not in visited:
                    stack.append(neighbor)

        return np.array(list(visited))

    def find_shortest_path(
        self,
        c_start: CoordTup,
        c_end: CoordTup,
    ) -> CoordArray:
        """find the shortest path between two coordinates, using A*"""
        c_start = tuple(c_start)
        c_end = tuple(c_end)

        g_score: dict[
            CoordTup, float
        ] = dict()  # cost of cheapest path to node from start currently known
        f_score: dict[CoordTup, float] = {
            c_start: 0.0
        }  # estimated total cost of path thru a node: f_score[c] := g_score[c] + heuristic(c, c_end)

        # init
        g_score[c_start] = 0.0
        g_score[c_start] = self.heuristic(c_start, c_end)

        closed_vtx: set[CoordTup] = set()  # nodes already evaluated
        open_vtx: set[CoordTup] = set([c_start])  # nodes to be evaluated
        source: dict[
            CoordTup, CoordTup
        ] = (
            dict()
        )  # node immediately preceding each node in the path (currently known shortest path)

        while open_vtx:
            # get lowest f_score node
            c_current: CoordTup = min(open_vtx, key=lambda c: f_score[c])
            # f_current: float = f_score[c_current]

            # check if goal is reached
            if c_end == c_current:
                path: list[CoordTup] = [c_current]
                p_current: CoordTup = c_current
                while p_current in source:
                    p_current = source[p_current]
                    path.append(p_current)
                return np.array(path[::-1])

            # close current node
            closed_vtx.add(c_current)
            open_vtx.remove(c_current)

            # update g_score of neighbors
            _np_neighbor: Coord
            for _np_neighbor in self.get_coord_neighbors(c_current):
                neighbor: CoordTup = tuple(_np_neighbor)

                if neighbor in closed_vtx:
                    # already checked
                    continue
                g_temp: float = g_score[c_current] + 1  # always 1 for maze neighbors

                if neighbor not in open_vtx:
                    # found new vtx, so add
                    open_vtx.add(neighbor)

                elif g_temp >= g_score[neighbor]:
                    # if already knew about this one, but current g_score is worse, skip
                    continue

                # store g_score and source
                source[neighbor] = c_current
                g_score[neighbor] = g_temp
                f_score[neighbor] = g_score[neighbor] + self.heuristic(neighbor, c_end)

    def get_nodes(self) -> CoordArray:
        """return a list of all nodes in the maze"""
        rows: Int[np.ndarray, "x y"]
        cols: Int[np.ndarray, "x y"]
        rows, cols = np.meshgrid(
            range(self.grid_shape[0]),
            range(self.grid_shape[1]),
            indexing="ij",
        )
        nodes: CoordArray = np.vstack((rows.ravel(), cols.ravel())).T
        return nodes

    def get_connected_component(self) -> CoordArray:
        """get the largest (and assumed only nonsingular) connected component of the maze

        TODO: other connected components?
        """
        if self.generation_meta.get("fully_connected", False):
            # for fully connected case, pick any two positions
            return self.get_nodes()
        else:
            # if not fully connected, pick two positions from the connected component
            visited_cells: set[CoordTup] | None = self.generation_meta.get(
                "visited_cells", None
            )
            if visited_cells is None:
                # TODO: dynamically generate visited_cells?
                raise ValueError(
                    f"a maze which is not marked as fully connected must have a visited_cells field in its generation_meta: {self.generation_meta}\n{self}\n{self.as_ascii()}"
                )
            else:
                visited_cells_np: Int[np.ndarray, "N 2"] = np.array(list(visited_cells))
                return visited_cells_np

    def generate_random_path(self) -> CoordArray:
        """return a path between randomly chosen start and end nodes within the connected component"""

        # we can't create a "path" in a single-node maze
        assert self.grid_shape[0] > 1 and self.grid_shape[1] > 1

        connected_component: CoordArray = self.get_connected_component()
        positions: Int[np.int8, "2 2"]
        if len(connected_component) < 2:
            warnings.warn(
                f"maze has only one node in its connected component:\n{connected_component=}\n{self.as_ascii()}"
            )
            assert len(connected_component) == 1
            # just connect it to itself
            positions = np.array([connected_component[0], connected_component[0]])
        else:
            positions = connected_component[
                np.random.choice(
                    len(connected_component),
                    size=2,
                    replace=False,
                )
            ]

        return self.find_shortest_path(positions[0], positions[1])

    # ============================================================
    # to and from adjacency list
    # ============================================================
    def as_adj_list(
        self, shuffle_d0: bool = True, shuffle_d1: bool = True
    ) -> NDArray["conn start_end coord", np.int8]:
        adj_list: NDArray["conn start_end coord", np.int8] = np.full(
            (self.n_connections, 2, 2),
            -1,
        )

        if shuffle_d1:
            flip_d1: NDArray["conn", np.float16] = np.random.rand(self.n_connections)

        # loop over all nonzero elements of the connection list
        i: int = 0
        for d, x, y in np.ndindex(self.connection_list.shape):
            if self.connection_list[d, x, y]:
                c_start: CoordTup = (x, y)
                c_end: CoordTup = (
                    x + (1 if d == 0 else 0),
                    y + (1 if d == 1 else 0),
                )
                adj_list[i, 0] = np.array(c_start)
                adj_list[i, 1] = np.array(c_end)

                # flip if shuffling
                if shuffle_d1 and (flip_d1[i] > 0.5):
                    c_s, c_e = adj_list[i, 0].copy(), adj_list[i, 1].copy()
                    adj_list[i, 0] = c_e
                    adj_list[i, 1] = c_s

                i += 1

        if shuffle_d0:
            np.random.shuffle(adj_list)

        return adj_list

    @classmethod
    def from_adj_list(
        cls,
        adj_list: NDArray["conn start_end coord", np.int8],
    ) -> "LatticeMaze":
        """create a LatticeMaze from a list of connections"""

        # Note: This has only been tested for square mazes. Might need to change some things if rectangular mazes are needed.
        grid_n: int = adj_list.max() + 1

        connection_list: ConnectionList = np.zeros(
            (2, grid_n, grid_n),
            dtype=np.bool_,
        )

        for c_start, c_end in adj_list:
            # check that exactly 1 coordinate matches
            if (c_start == c_end).sum() != 1:
                raise ValueError("invalid connection")

            # get the direction
            d: int = (c_start != c_end).argmax()

            x: int
            y: int
            # pick whichever has the lesser value in the direction `d`
            if c_start[d] < c_end[d]:
                x, y = c_start
            else:
                x, y = c_end

            connection_list[d, x, y] = True

        return LatticeMaze(
            connection_list=connection_list,
        )

    def as_adj_list_tokens(self, node_token_map: dict[CoordTup, str]) -> list[str]:
        return [
            SPECIAL_TOKENS["adj_list_start"],
            *chain.from_iterable(
                [
                    [
                        node_token_map[tuple(c_s.tolist())],
                        SPECIAL_TOKENS["connector"],
                        node_token_map[tuple(c_e.tolist())],
                        SPECIAL_TOKENS["adjacency_endline"],
                    ]
                    for c_s, c_e in self.as_adj_list()
                ]
            ),
            SPECIAL_TOKENS["adj_list_end"],
        ]

    def as_tokens(
        self,
        node_token_map: dict[CoordTup, str],
    ) -> list[str]:
        """serialize maze and solution to tokens"""
        tokens: list[str] = self.as_adj_list_tokens(node_token_map)
        # if getattr(self, "start_pos", None) is not None:
        if isinstance(self, TargetedLatticeMaze):
            tokens += self.get_start_pos_tokens(node_token_map)
        if isinstance(self, TargetedLatticeMaze):
            tokens += self.get_end_pos_tokens(node_token_map)
        if isinstance(self, SolvedMaze):
            tokens += self.get_solution_tokens(node_token_map)

        return tokens

    @classmethod
    def from_tokens(cls, tokens: list[str]) -> "LatticeMaze":
        """create a LatticeMaze from a list of tokens"""
        if tokens[0] == SPECIAL_TOKENS["adj_list_start"]:
            adj_list_tokens = get_adj_list_tokens(tokens)
        else:
            # If we're not getting a "complete" tokenized maze, assume it's a list of coord tokens already
            adj_list_tokens = tokens

        edges: list[str] = list_split(
            adj_list_tokens,
            SPECIAL_TOKENS["adjacency_endline"],
        )

        coordinates: list[tuple[str, str]] = list()
        # what we're doing here:
        # for each edge, convert to a coord tuple and add it to the list
        # check that for each edge we have a connector, and a single token on each side
        for e in edges:
            # skip last endline
            if len(e) != 0:
                # split into start and end
                e_split: list[list] = list_split(e, SPECIAL_TOKENS["connector"])
                # print(f"{e = } {e_split = }")
                assert len(e_split) == 2, f"invalid edge: {e = } {e_split = }"
                assert all(
                    len(c) == 1 for c in e_split
                ), f"invalid edge: {e = } {e_split = }"
                coordinates.append(tuple([c[0] for c in e_split]))

        assert all(
            len(c) == 2 for c in coordinates
        ), f"invalid coordinates: {coordinates = }"

        adj_list: NDArray["conn start_end coord", np.int8] = np.full(
            (len(coordinates), 2, 2),
            -1,
        )

        for i, (c_start, c_end) in enumerate(coordinates):
            adj_list[i, 0] = np.array(coord_str_to_tuple(c_start))
            adj_list[i, 1] = np.array(coord_str_to_tuple(c_end))

        return cls.from_adj_list(adj_list)

    # ============================================================
    # to and from pixels
    # ============================================================
    def _as_pixels_bw(self) -> BinaryPixelGrid:
        assert self.lattice_dim == 2, "only 2D mazes are supported"
        # Create an empty pixel grid with walls
        pixel_grid: Int[np.ndarray, "x y"] = np.full(
            (self.grid_shape[0] * 2 + 1, self.grid_shape[1] * 2 + 1),
            False,
            dtype=np.bool_,
        )

        # Set white nodes
        pixel_grid[1::2, 1::2] = True

        # Set white connections (downward)
        for i, row in enumerate(self.connection_list[0]):
            for j, connected in enumerate(row):
                if connected:
                    pixel_grid[i * 2 + 2, j * 2 + 1] = True

        # Set white connections (rightward)
        for i, row in enumerate(self.connection_list[1]):
            for j, connected in enumerate(row):
                if connected:
                    pixel_grid[i * 2 + 1, j * 2 + 2] = True

        return pixel_grid

    def as_pixels(
        self,
        show_endpoints: bool = True,
        show_solution: bool = True,
    ) -> PixelGrid:
        if show_solution and not show_endpoints:
            raise ValueError("show_solution=True requires show_endpoints=True")
        # convert original bool pixel grid to RGB
        pixel_grid_bw: BinaryPixelGrid = self._as_pixels_bw()
        pixel_grid: PixelGrid = np.full(
            (*pixel_grid_bw.shape, 3), PixelColors.WALL, dtype=np.uint8
        )
        pixel_grid[pixel_grid_bw == True] = PixelColors.OPEN

        if self.__class__ == LatticeMaze:
            return pixel_grid

        # set endpoints for TargetedLatticeMaze
        if self.__class__ == TargetedLatticeMaze:
            if show_endpoints:
                pixel_grid[
                    self.start_pos[0] * 2 + 1, self.start_pos[1] * 2 + 1
                ] = PixelColors.START
                pixel_grid[
                    self.end_pos[0] * 2 + 1, self.end_pos[1] * 2 + 1
                ] = PixelColors.END
            return pixel_grid

        # set solution
        if show_solution:
            for coord in self.solution:
                pixel_grid[coord[0] * 2 + 1, coord[1] * 2 + 1] = PixelColors.PATH

            # set pixels between coords
            for index, coord in enumerate(self.solution[:-1]):
                next_coord = self.solution[index + 1]
                # check they are adjacent using norm
                assert (
                    np.linalg.norm(np.array(coord) - np.array(next_coord)) == 1
                ), f"Coords {coord} and {next_coord} are not adjacent"
                # set pixel between them
                pixel_grid[
                    coord[0] * 2 + 1 + next_coord[0] - coord[0],
                    coord[1] * 2 + 1 + next_coord[1] - coord[1],
                ] = PixelColors.PATH

            # set endpoints (again, since path would overwrite them)
            pixel_grid[
                self.start_pos[0] * 2 + 1, self.start_pos[1] * 2 + 1
            ] = PixelColors.START
            pixel_grid[
                self.end_pos[0] * 2 + 1, self.end_pos[1] * 2 + 1
            ] = PixelColors.END

        return pixel_grid

    @classmethod
    def _from_pixel_grid_bw(
        cls, pixel_grid: BinaryPixelGrid
    ) -> tuple[ConnectionList, tuple[int, int]]:
        grid_shape: tuple[int, int] = (
            pixel_grid.shape[0] // 2,
            pixel_grid.shape[1] // 2,
        )
        connection_list: ConnectionList = np.zeros((2, *grid_shape), dtype=np.bool_)

        # Extract downward connections
        connection_list[0] = pixel_grid[2::2, 1::2]

        # Extract rightward connections
        connection_list[1] = pixel_grid[1::2, 2::2]

        return connection_list, grid_shape

    @classmethod
    def _from_pixel_grid_with_positions(
        cls,
        pixel_grid: PixelGrid | BinaryPixelGrid,
        marked_positions: dict[str, RGB],
    ) -> tuple[ConnectionList, tuple[int, int], dict[str, CoordArray]]:
        # Convert RGB pixel grid to Bool pixel grid
        pixel_grid_bw: BinaryPixelGrid = ~np.all(
            pixel_grid == PixelColors.WALL, axis=-1
        )
        connection_list: ConnectionList
        grid_shape: tuple[int, int]
        connection_list, grid_shape = cls._from_pixel_grid_bw(pixel_grid_bw)

        # Find any marked positions
        out_positions: dict[str, CoordArray] = dict()
        for key, color in marked_positions.items():
            pos_temp: Int[np.ndarray, "x y"] = np.argwhere(
                np.all(pixel_grid == color, axis=-1)
            )
            pos_save: list[CoordTup] = list()
            for pos in pos_temp:
                # if it is a coordinate and not connection (transform position, %2==1)
                if pos[0] % 2 == 1 and pos[1] % 2 == 1:
                    pos_save.append((pos[0] // 2, pos[1] // 2))

            out_positions[key] = np.array(pos_save)

        return connection_list, grid_shape, out_positions

    @classmethod
    def from_pixels(
        cls,
        pixel_grid: PixelGrid,
    ) -> "LatticeMaze":
        connection_list: ConnectionList
        grid_shape: tuple[int, int]

        # if a binary pixel grid, return regular LaticeMaze
        if len(pixel_grid.shape) == 2:
            connection_list, grid_shape = cls._from_pixel_grid_bw(pixel_grid)
            return LatticeMaze(connection_list=connection_list)

        # otherwise, detect and check it's valid
        cls_detected: typing.Type[LatticeMaze] = detect_pixels_type(pixel_grid)
        if not cls in cls_detected.__mro__:
            raise ValueError(
                f"Pixel grid cannot be cast to {cls.__name__}, detected type {cls_detected.__name__}"
            )

        (
            connection_list,
            grid_shape,
            marked_pos,
        ) = cls._from_pixel_grid_with_positions(
            pixel_grid=pixel_grid,
            marked_positions=dict(
                start=PixelColors.START, end=PixelColors.END, solution=PixelColors.PATH
            ),
        )
        # if we wanted a LatticeMaze, return it
        if cls == LatticeMaze:
            return LatticeMaze(connection_list=connection_list)

        # otherwise, keep going
        temp_maze: LatticeMaze = LatticeMaze(connection_list=connection_list)

        # start and end pos
        start_pos_arr, end_pos_arr = marked_pos["start"], marked_pos["end"]
        assert start_pos_arr.shape == (
            1,
            2,
        ), f"start_pos_arr {start_pos_arr} has shape {start_pos_arr.shape}, expected shape (1, 2) -- a single coordinate"
        assert end_pos_arr.shape == (
            1,
            2,
        ), f"end_pos_arr {end_pos_arr} has shape {end_pos_arr.shape}, expected shape (1, 2) -- a single coordinate"

        start_pos: Coord = start_pos_arr[0]
        end_pos: Coord = end_pos_arr[0]

        # return a TargetedLatticeMaze if that's what we wanted
        if cls == TargetedLatticeMaze:
            return TargetedLatticeMaze(
                connection_list=connection_list,
                start_pos=start_pos,
                end_pos=end_pos,
            )

        # raw solution, only contains path elements and not start or end
        solution_raw: CoordArray = marked_pos["solution"]
        if len(solution_raw.shape) == 2:
            assert (
                solution_raw.shape[1] == 2
            ), f"solution {solution_raw} has shape {solution_raw.shape}, expected shape (n, 2)"
        elif solution_raw.shape == (0,):
            # the solution and end should be immediately adjacent
            assert (
                np.sum(np.abs(start_pos - end_pos)) == 1
            ), f"start_pos {start_pos} and end_pos {end_pos} are not adjacent, but no solution was given"

        # order the solution, by creating a list from the start to the end
        # add end pos, since we will iterate over all these starting from the start pos
        solution_raw_list: list[CoordTup] = [tuple(c) for c in solution_raw] + [
            tuple(end_pos)
        ]
        # solution starts with start point
        solution: list[CoordTup] = [tuple(start_pos)]
        while solution[-1] != tuple(end_pos):
            # use `get_coord_neighbors` to find connected neighbors
            neighbors: CoordArray = temp_maze.get_coord_neighbors(solution[-1])
            # TODO: make this less ugly
            assert (len(neighbors.shape) == 2) and (
                neighbors.shape[1] == 2
            ), f"neighbors {neighbors} has shape {neighbors.shape}, expected shape (n, 2)\n{neighbors = }\n{solution = }\n{solution_raw = }\n{temp_maze.as_ascii()}"
            # neighbors = neighbors[:, [1, 0]]
            # filter out neighbors that are not in the raw solution
            neighbors_filtered: CoordArray = np.array(
                [
                    coord
                    for coord in neighbors
                    if (
                        tuple(coord) in solution_raw_list
                        and not tuple(coord) in solution
                    )
                ]
            )
            # assert only one element is left, and then add it to the solution
            assert neighbors_filtered.shape == (
                1,
                2,
            ), f"neighbors_filtered has shape {neighbors_filtered.shape}, expected shape (1, 2)\n{neighbors = }\n{neighbors_filtered = }\n{solution = }\n{solution_raw_list = }\n{temp_maze.as_ascii()}"
            solution.append(tuple(neighbors_filtered[0]))

        # assert the solution is complete
        assert solution[0] == tuple(
            start_pos
        ), f"solution {solution} does not start at start_pos {start_pos}"
        assert solution[-1] == tuple(
            end_pos
        ), f"solution {solution} does not end at end_pos {end_pos}"

        return cls(
            connection_list=np.array(connection_list),
            solution=np.array(solution),
        )

    # ============================================================
    # to and from ASCII
    # ============================================================
    def _as_ascii_grid(self) -> Shaped[np.ndarray, "x y"]:
        # Get the pixel grid using to_pixels().
        pixel_grid: Bool[np.ndarray, "x y"] = self._as_pixels_bw()

        # Replace pixel values with ASCII characters.
        ascii_grid: Shaped[np.ndarray, "x y"] = np.full(
            pixel_grid.shape, AsciiChars.WALL, dtype=str
        )
        ascii_grid[pixel_grid == True] = AsciiChars.OPEN

        return ascii_grid

    def as_ascii(
        self,
        show_endpoints: bool = True,
        show_solution: bool = True,
    ) -> str:
        """return an ASCII grid of the maze"""
        ascii_grid: Shaped[np.ndarray, "x y"] = self._as_ascii_grid()
        pixel_grid: PixelGrid = self.as_pixels(
            show_endpoints=show_endpoints, show_solution=show_solution
        )

        chars_replace: tuple = tuple()
        if show_endpoints:
            chars_replace += (AsciiChars.START, AsciiChars.END)
        if show_solution:
            chars_replace += (AsciiChars.PATH,)

        for ascii_char, pixel_color in ASCII_PIXEL_PAIRINGS.items():
            if ascii_char in chars_replace:
                ascii_grid[(pixel_grid == pixel_color).all(axis=-1)] = ascii_char

        return "\n".join("".join(row) for row in ascii_grid)

    @classmethod
    def from_ascii(cls, ascii_str: str) -> "LatticeMaze":
        lines: list[str] = ascii_str.strip().split("\n")
        ascii_grid: Shaped[np.ndarray, "x y"] = np.array(
            [list(line) for line in lines], dtype=str
        )
        pixel_grid: PixelGrid = np.zeros((*ascii_grid.shape, 3), dtype=np.uint8)

        for ascii_char, pixel_color in ASCII_PIXEL_PAIRINGS.items():
            pixel_grid[ascii_grid == ascii_char] = pixel_color

        return cls.from_pixels(pixel_grid)


@serializable_dataclass(frozen=True, kw_only=True)
class TargetedLatticeMaze(LatticeMaze):
    """A LatticeMaze with a start and end position"""

    # this jank is so that SolvedMaze can inherit from this class without needing arguments for start_pos and end_pos
    start_pos: Coord
    end_pos: Coord

    def __post_init__(self) -> None:
        # make things numpy arrays (very jank to override frozen dataclass)
        self.__dict__["start_pos"] = np.array(self.start_pos)
        self.__dict__["end_pos"] = np.array(self.end_pos)
        assert self.start_pos is not None
        assert self.end_pos is not None
        # check that start and end are in bounds
        if (
            self.start_pos[0] >= self.grid_shape[0]
            or self.start_pos[1] >= self.grid_shape[1]
        ):
            raise ValueError(
                f"start_pos {self.start_pos} is out of bounds for grid shape {self.grid_shape}"
            )
        if (
            self.end_pos[0] >= self.grid_shape[0]
            or self.end_pos[1] >= self.grid_shape[1]
        ):
            raise ValueError(
                f"end_pos {self.end_pos} is out of bounds for grid shape {self.grid_shape}"
            )

    def get_start_pos_tokens(self, node_token_map: dict[CoordTup, str]) -> list[str]:
        return [
            SPECIAL_TOKENS["origin_start"],
            node_token_map[tuple(self.start_pos)],
            SPECIAL_TOKENS["origin_end"],
        ]

    def get_end_pos_tokens(self, node_token_map: dict[CoordTup, str]) -> list[str]:
        return [
            SPECIAL_TOKENS["target_start"],
            node_token_map[tuple(self.end_pos)],
            SPECIAL_TOKENS["target_end"],
        ]

    @classmethod
    def from_lattice_maze(
        cls,
        lattice_maze: LatticeMaze,
        start_pos: Coord,
        end_pos: Coord,
    ) -> "TargetedLatticeMaze":
        return cls(
            connection_list=lattice_maze.connection_list,
            start_pos=start_pos,
            end_pos=end_pos,
            generation_meta=lattice_maze.generation_meta,
        )


@serializable_dataclass(frozen=True, kw_only=True)
class SolvedMaze(TargetedLatticeMaze):
    """Stores a maze and a solution"""

    solution: CoordArray

    def __init__(
        self,
        connection_list: ConnectionList,
        solution: CoordArray,
        generation_meta: dict | None = None,
        start_pos: Coord | None = None,
        end_pos: Coord | None = None,
    ) -> None:
        super().__init__(
            connection_list=connection_list,
            generation_meta=generation_meta,
            start_pos=np.array(solution[0]),
            end_pos=np.array(solution[-1]),
        )
        self.__dict__["solution"] = solution

        if start_pos is not None:
            assert np.array_equal(
                np.array(start_pos), self.start_pos
            ), f"when trying to create a SolvedMaze, the given start_pos does not match the one in the solution: given={start_pos}, solution={self.start_pos}"
        if end_pos is not None:
            assert np.array_equal(
                np.array(end_pos), self.end_pos
            ), f"when trying to create a SolvedMaze, the given end_pos does not match the one in the solution: given={end_pos}, solution={self.end_pos}"

    def __hash__(self) -> int:
        return hash((self.connection_list.tobytes(), self.solution.tobytes()))

    def get_solution_tokens(self, node_token_map: dict[CoordTup, str]) -> list[str]:
        return [
            SPECIAL_TOKENS["path_start"],
            *[node_token_map[tuple(c.tolist())] for c in self.solution],
            SPECIAL_TOKENS["path_end"],
        ]

    # for backwards compatibility
    @property
    def maze(self) -> LatticeMaze:
        warnings.warn(
            "maze is deprecated, SolvedMaze now inherits from LatticeMaze.",
            DeprecationWarning,
        )
        return LatticeMaze(connection_list=self.connection_list)

    @classmethod
    def from_lattice_maze(
        cls, lattice_maze: LatticeMaze, solution: list[CoordTup]
    ) -> "SolvedMaze":
        return cls(
            connection_list=lattice_maze.connection_list,
            solution=solution,
            generation_meta=lattice_maze.generation_meta,
        )

    @classmethod
    def from_targeted_lattice_maze(
        cls, targeted_lattice_maze: TargetedLatticeMaze
    ) -> "SolvedMaze":
        """solves the given targeted lattice maze and returns a SolvedMaze"""
        solution: list[CoordTup] = targeted_lattice_maze.find_shortest_path(
            targeted_lattice_maze.start_pos,
            targeted_lattice_maze.end_pos,
        )
        return cls(
            connection_list=targeted_lattice_maze.connection_list,
            solution=np.array(solution),
            generation_meta=targeted_lattice_maze.generation_meta,
        )

    @classmethod
    def from_tokens(cls, tokens: list[str] | str, data_cfg) -> "SolvedMaze":
        if type(tokens) == str:
            tokens = tokens.split(" ")

        maze: LatticeMaze = LatticeMaze.from_tokens(tokens)
        path_tokens: list[str] = get_path_tokens(tokens)
        solution: list[str | tuple[int, int]] = tokens_to_coords(path_tokens, data_cfg)

        assert len(solution) > 0, f"No solution found: {solution = }"

        try:
            solution_cast: list[CoordTup] = cast(list[CoordTup], solution)
            solution_np: CoordArray = np.array(solution_cast)
        except ValueError as e:
            raise ValueError(f"Invalid solution: {solution = }") from e

        return cls.from_lattice_maze(lattice_maze=maze, solution=solution_np)


def detect_pixels_type(data: PixelGrid) -> typing.Type[LatticeMaze]:
    """Detects the type of pixels data by checking for the presence of start and end pixels"""
    if color_in_pixel_grid(data, PixelColors.START) or color_in_pixel_grid(
        data, PixelColors.END
    ):
        if color_in_pixel_grid(data, PixelColors.PATH):
            return SolvedMaze
        else:
            return TargetedLatticeMaze
    else:
        return LatticeMaze