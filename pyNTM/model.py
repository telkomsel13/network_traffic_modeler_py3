"""
A class that defines the network being modeled and that contains all
modeled objects in the network such as Nodes, Interfaces, Circuits,
and Demands.

Allows a single connection (Circuit) between layer3 Nodes.  If multiple
Circuits between Nodes is needed, use Parallel_Link_Model object.

This Model object will generally perform better than Parallel_Model_Object due
to the latter's requirement to check for multiple Circuits between Nodes.
"""

from pprint import pprint

import networkx as nx

from .circuit import Circuit
from .interface import Interface
from .exceptions import ModelException
from .master_model import MasterModel
from .utilities import find_end_index
from .node import Node
from .rsvp import RSVP_LSP
from .srlg import SRLG

# TODO - call to analyze model for Unrouted LSPs and LSPs not on shortest path
# TODO - add simulation summary output with # failed nodes, interfaces, srlgs, unrouted lsp/demands,
#  routed lsp/demands in dict form
# TODO - look at removing the requirement that Interface circuit_id be specified since the remote side
#  can be determined because only one circuit can exist between any pair of Nodes
# TODO - add support for SRLGs in load_model_file
# TODO - add attribute for Node/Interface whereby an object can be failed by itself
#  and not unfail when a parent SRLG unfails


class Model(MasterModel):
    """A network model object consisting of the following base components:
        - Interface objects (set): layer 3 Node interfaces.  Interfaces have a
          'capacity' attribute that determines how much traffic it can carry.
          Note: Interfaces are matched into Circuit objects based on the
          interface circuit_ids --> A pair of Interfaces with the same circuit_id
          value get matched into a Circuit
        - Node objects (set): vertices on the network (aka 'layer 3 devices')
          that contain Interface objects.  Nodes are connected to each other
          via a pair of matched Interfaces (Circuits)
        - Demand objects (set): traffic loads on the network.  Each demand starts
          from a source node and transits the network to a destination node.
          A demand also has a magnitude, representing how much traffic it
          is carrying.  The demand's magnitude will apply against each
          interface's available capacity
        - RSVP LSP objects (set): RSVP LSPs in the Model
        - Circuit objects are created by matching Interface objects
    """

    def __init__(self, interface_objects=set(), node_objects=set(),
                 demand_objects=set(), rsvp_lsp_objects=set()):
        self.interface_objects = interface_objects
        self.node_objects = node_objects
        self.demand_objects = demand_objects
        self.circuit_objects = set()
        self.rsvp_lsp_objects = rsvp_lsp_objects
        self.srlg_objects = set()
        self._parallel_lsp_groups = {}

        super().__init__(interface_objects, node_objects, demand_objects, rsvp_lsp_objects)

    def __repr__(self):
        return 'Model(Interfaces: %s, Nodes: %s, Demands: %s, RSVP_LSPs: %s)' % (len(self.interface_objects),
                                                                                 len(self.node_objects),
                                                                                 len(self.demand_objects),
                                                                                 len(self.rsvp_lsp_objects))

    def add_network_interfaces_from_list(self, network_interfaces):
        """
        A tool that reads network interface info and updates an *existing* model.
        Intended to be used from CLI/interactive environment
        Interface info must be a list of dicts and in format like below example:
            network_interfaces = [
            {'name':'A-to-B', 'cost':4,'capacity':100, 'node':'A',
            'remote_node': 'B', 'circuit_id': 1, 'failed': False},
            {'name':'A-to-Bv2', 'cost':40,'capacity':150, 'node':'A',
            'remote_node': 'B', 'circuit_id': 2, 'failed': False},
            {'name':'A-to-C', 'cost':1,'capacity':200, 'node':'A',
            'remote_node': 'C', 'circuit_id': 3, 'failed': False},]
        """

        new_interface_objects, new_node_objects = \
            self._make_network_interfaces(network_interfaces)
        self.node_objects = self.node_objects.union(new_node_objects)
        self.interface_objects = \
            self.interface_objects.union(new_interface_objects)
        self.validate_model()

    def validate_model(self):
        """
        Validates that data fed into the model creates a valid network model
        """

        # create circuits table, flags ints that are not part of a circuit
        circuits = self._make_circuits(return_exception=True)

        # Make dict to hold interface data, each entry has the following
        # format:
        # {'lsps': [], 'reserved_bandwidth': 0}
        int_info = self._make_int_info_dict()

        # Interface reserved bandwidth error sets
        int_res_bw_too_high = set([])
        int_res_bw_sum_error = set([])

        error_data = []  # list of all errored checks

        for interface in (interface for interface in self.interface_objects):  # pragma: no cover
            self._reserved_bw_error_checks(int_info, int_res_bw_sum_error, int_res_bw_too_high, interface)

        # If creation of circuits returns a dict, there are problems
        if isinstance(circuits, dict):  # pragma: no cover
            error_data.append({'ints_w_no_remote_int': circuits['data']})

        # Append any failed checks to error_data
        if len(int_res_bw_too_high) > 0:  # pragma: no cover
            error_data.append({'int_res_bw_too_high': int_res_bw_too_high})

        if len(int_res_bw_sum_error) > 0:  # pragma: no cover
            error_data.append({'int_res_bw_sum_error': int_res_bw_sum_error})

        # Validate there are no duplicate interfaces
        unique_interfaces_per_node = self._unique_interface_per_node()

        # Log any duplicate interfaces on a node
        if not unique_interfaces_per_node:  # pragma: no cover
            error_data.append(unique_interfaces_per_node)

        # Make validate_model() check for matching failed statuses
        # on the interfaces and matching interface capacity
        circuits_with_mismatched_interface_capacity = []
        for ckt in (ckt for ckt in self.circuit_objects):
            self._validate_circuit_interface_capacity(circuits_with_mismatched_interface_capacity, ckt)

        if len(circuits_with_mismatched_interface_capacity) > 0:
            int_status_error_dict = {
                'circuits_with_mismatched_interface_capacity':
                circuits_with_mismatched_interface_capacity
            }
            error_data.append(int_status_error_dict)

        # Look for multiple links between nodes (not allowed in Model)
        if len(self.multiple_links_between_nodes()) > 0:
            multiple_links_between_nodes = {}
            multiple_links_between_nodes['multiple links between nodes detected; not allowed in Model object'
                                         '(use Parallel_Link_Model)'] = self.multiple_links_between_nodes()
            error_data.append(multiple_links_between_nodes)

        srlg_errors = self.validate_srlg_nodes()

        if len(srlg_errors) > 0:
            error_data.append(srlg_errors)

        # Verify no duplicate nodes
        node_names = set([node.name for node in self.node_objects])
        if (len(self.node_objects)) != (len(node_names)):  # pragma: no cover
            node_dict = {'len_node_objects': len(self.node_objects),
                         'len_node_names': len(node_names)}
            error_data.append(node_dict)

        # Read error_data
        if len(error_data) > 0:
            message = 'network interface validation failed, see returned data'
            pprint(message)
            pprint(error_data)
            raise ModelException((message, error_data))
        else:
            return self

    def validate_srlg_nodes(self):
        """
        Validate that Nodes in each SRLG have the SRLG in their srlgs set.
        srlg_errors is a dict of node names as keys and a list of SRLGs that node is
        a member of in the model but that the SRLG is not in node.srlgs
        :return: dict where keys are Node names and values are lists of SRLG names;
        each value will be a single list of SRLG names missing that Node in the
        SRLG node set
        """

        srlg_errors = {}
        for srlg in self.srlg_objects:  # pragma: no cover  # noqa  # TODO - perhaps cover this later in unit testing
            nodes_in_srlg_but_srlg_not_in_node_srlgs = [node for node in srlg.node_objects if srlg not in node.srlgs]
            for node in nodes_in_srlg_but_srlg_not_in_node_srlgs:
                try:
                    srlg_errors[node.name].append(srlg.name)
                except KeyError:
                    srlg_errors[node.name] = []
        return srlg_errors

    def update_simulation(self):
        """
        Updates the simulation state; this needs to be run any time there is
        a change to the state of the Model, such as failing an interface, adding
        a Demand, adding/removing and LSP, etc.
        This call does not carry forward any state from the previous simulation
        results.
        """

        self._parallel_lsp_groups = {}  # Reset the attribute

        # This set of interfaces can be used to route traffic
        non_failed_interfaces = set()
        # This set of nodes can be used to route traffic
        available_nodes = set()

        # Find all the non-failed interfaces in the model and
        # add them to non_failed_interfaces.
        # If the interface is not failed, then by definition, the nodes are
        # not failed
        for interface_object in (interface_object for interface_object in self.interface_objects
                                 if interface_object.failed is not True):
            non_failed_interfaces.add(interface_object)
            available_nodes.add(interface_object.node_object)
            available_nodes.add(interface_object.remote_node_object)

        # Create a model consisting only of the non-failed interfaces and
        # corresponding non-failed (available) nodes
        non_failed_interfaces_model = Model(non_failed_interfaces,
                                            available_nodes, self.demand_objects,
                                            self.rsvp_lsp_objects)

        # Reset the reserved_bandwidth, traffic on each interface
        for interface in (interface for interface in self.interface_objects):
            interface.reserved_bandwidth = 0
            interface.traffic = 0

        for lsp in (lsp for lsp in self.rsvp_lsp_objects):
            lsp.path = 'Unrouted'

        for demand in (demand for demand in self.demand_objects):
            demand.path = 'Unrouted'

        print("Routing the LSPs . . . ")
        # Route the RSVP LSPs
        self = self._route_lsps(non_failed_interfaces_model)
        print("LSPs routed (if present); routing demands now . . .")
        # Route the demands
        self = self._route_demands(self.demand_objects,
                                   non_failed_interfaces_model)
        print("Demands routed; validating model . . . ")

        self.validate_model()

    def _make_circuits(self, return_exception=True, include_failed_circuits=True):
        """
        Matches interface objects into circuits and returns the circuits list
        :param return_exception: Should an exception be returned if not all the
                                 interfaces can be matched into a circuit?
        :param include_failed_circuits:  Should circuits that will be in a
                                         failed state be created?
        :return: a set of Circuit objects in the Model, each Circuit
                 comprised of two Interface objects
        """

        G = self._make_weighted_network_graph(include_failed_circuits=include_failed_circuits)

        # Determine which interfaces pair up into good circuits in G
        paired_interfaces = ((local_node_name, remote_node_name, data) for
                             (local_node_name, remote_node_name, data) in
                             G.edges(data=True) if G.has_edge(remote_node_name,
                                                              local_node_name))

        # Set interface object in_ckt = False and baseline the circuit_id
        for interface in (interface for interface in self.interface_objects):
            interface.in_ckt = False
        circuit_id_number = 1
        circuits = set([])

        # Using the paired interfaces (source_node, dest_node) pairs from G,
        # get the corresponding interface objects from the model to create
        # the circuit object
        for interface in (interface for interface in paired_interfaces):
            # Get each interface from model for each
            int1 = self.get_interface_object_from_nodes(interface[0],
                                                        interface[1])
            int2 = self.get_interface_object_from_nodes(interface[1],
                                                        interface[0])

            if int1.in_ckt is False and int2.in_ckt is False:
                # Mark interface objects as in_ckt = True
                int1.in_ckt = True
                int2.in_ckt = True

                # Add circuit_id to interface objects
                int1.circuit_id = circuit_id_number
                int2.circuit_id = circuit_id_number
                circuit_id_number = circuit_id_number + 1

                ckt = Circuit(int1, int2)
                circuits.add(ckt)

        # Find any interfaces that don't have counterpart
        exception_ints_not_in_ckt = [(local_node_name, remote_node_name, data)
                                     for (local_node_name, remote_node_name, data) in
                                     G.edges(data=True) if not (G.has_edge(remote_node_name, local_node_name))]

        if len(exception_ints_not_in_ckt) > 0:
            exception_msg = ('WARNING: These interfaces were not matched '
                             'into a circuit {}'.format(exception_ints_not_in_ckt))
            if return_exception:
                raise ModelException(exception_msg)
            else:
                return {'data': exception_ints_not_in_ckt}

        self.circuit_objects = circuits

    def get_interface_object_from_nodes(self, local_node_name, remote_node_name):
        """Returns an Interface object with the specified local and
        remote node names """
        for interface in (interface for interface in self.interface_objects):
            if interface.node_object.name == local_node_name and \
                    interface.remote_node_object.name == remote_node_name:
                return interface

    def add_circuit(self, node_a_object, node_b_object, node_a_interface_name,
                    node_b_interface_name, cost_intf_a=1, cost_intf_b=1,
                    capacity=1000, failed=False, circuit_id=None):
        """
        Creates component Interface objects for a new Circuit in the Model.
        The Circuit object will then be created during the validate_model() call.
        :param node_a_object: Node object
        :param node_b_object: Node object
        :param node_a_interface_name: name of component Interface on node_a
        :param node_b_interface_name: name of component Interface on node_b
        :param cost_intf_a: metric/cost of node_a_interface component Interface
        :param cost_intf_b: metric/cost of node_b_interface component Interface
        :param capacity: Circuit's capacity
        :param failed: Should the Circuit be created in a Failed state?
        :param circuit_id: Optional.  Will be auto-assigned unless specified
        :return: Model with new Circuit comprised of 2 new Interfaces
        """

        if circuit_id is None:
            circuit_ids = self.all_interface_circuit_ids
            if len(circuit_ids) == 0:
                circuit_id = 1
            else:
                circuit_id = max(circuit_ids) + 1

        int_a = Interface(node_a_interface_name, cost_intf_a, capacity,
                          node_a_object, node_b_object, circuit_id)
        int_b = Interface(node_b_interface_name, cost_intf_b, capacity,
                          node_b_object, node_a_object, circuit_id)

        existing_int_keys = set([interface._key for interface in self.interface_objects])

        if int_a._key in existing_int_keys:
            raise ModelException("interface {} on node {} already exists in model".format(int_a, node_a_object))
        elif int_b._key in existing_int_keys:
            raise ModelException("interface {} on node {} already exists in model".format(int_b, node_b_object))

        self.interface_objects.add(int_a)
        self.interface_objects.add(int_b)

        self.validate_model()

    def is_node_an_orphan(self, node_object):
        """Determines if a node is in orphan_nodes"""
        if node_object in self.get_orphan_node_objects():
            return True
        else:
            return False

    def get_orphan_node_objects(self):
        """
        Returns list of Nodes that have no interfaces
        """
        orphan_nodes = [node for node in self.node_objects if len(node.interfaces(self)) == 0]

        return orphan_nodes

    def add_node(self, node_object):
        """
        Adds a node object to the model object
        """

        if node_object.name in (node.name for node in self.node_objects):
            message = "A node with name {} already exists in the model".format(node_object.name)
            raise ModelException(message)
        else:
            self.node_objects.add(node_object)

        self.validate_model()

    def get_node_object(self, node_name):
        """
        Returns a Node object, given a node's name
        """
        matching_node = [node for node in self.node_objects if node.name == node_name]

        if len(matching_node) > 0:
            return matching_node[0]
        else:
            message = "No node with name %s exists in the model" % node_name
            raise ModelException(message)

    def _make_network_interfaces(self, interface_info_list):
        """
        Returns set of Interface objects and a set of Node objects for Nodes
        that are not already in the Model.
        :param interface_info_list: list of dicts with interface specs;
        :return: Set of Interface objects and set of Node objects for the
                 new Interfaces for Nodes that are not already in the model
        """
        network_interface_objects = set([])
        network_node_objects = set([])

        # Create the Interface objects
        for interface in interface_info_list:
            intf = Interface(interface['name'], interface['cost'],
                             interface['capacity'], Node(interface['node']),
                             Node(interface['remote_node']),
                             interface['circuit_id'])
            network_interface_objects.add(intf)

            # Check to see if the Interface's Node already exists, if not, add it
            node_names = ([node.name for node in self.node_objects])
            if interface['node'] not in node_names:
                network_node_objects.add(Node(interface['node']))
            if interface['remote_node'] not in node_names:
                network_node_objects.add(Node(interface['remote_node']))

        return (network_interface_objects, network_node_objects)

    def add_rsvp_lsp(self, source_node_name, dest_node_name, name):
        """
        Adds an RSVP LSP with name from the source node to the
        dest node and validates model.
        :param source_node_name: LSP source Node name
        :param dest_node_name: LSP destination Node name
        :param name: name of LSP
        :return: A validated Model with the new RSVP_LSP object
        """
        source_node_object = self.get_node_object(source_node_name)
        dest_node_object = self.get_node_object(dest_node_name)
        added_lsp = RSVP_LSP(source_node_object, dest_node_object, name)

        if added_lsp._key in set([lsp._key for lsp in self.rsvp_lsp_objects]):
            message = '{} already exists in rsvp_lsp_objects'.format(added_lsp)
            raise ModelException(message)
        self.rsvp_lsp_objects.add(added_lsp)

        self.validate_model()

    def get_demand_object(self, source_node_name, dest_node_name, demand_name='none'):
        """
        Returns demand specified by the source_node_name, dest_node_name, name;
        throws exception if demand not found
        """
        model_demand_iterator = (demand for demand in self.demand_objects)

        demand_to_return = None

        for demand in model_demand_iterator:
            if demand.source_node_object.name == source_node_name and \
                    demand.dest_node_object.name == dest_node_name and \
                    demand.name == demand_name:
                demand_to_return = demand
                return demand_to_return

        if demand_to_return is None:
            raise ModelException('no matching demand')

    def get_rsvp_lsp(self, source_node_name, dest_node_name, lsp_name='none'):
        """
        Returns the RSVP LSP from the model with the specified source node
        name, dest node name, and LSP name.
        :param source_node_name: name of source node for LSP
        :param dest_node_name: name of destination node for LSP
        :param lsp_name: name of LSP
        :return: RSVP_LSP object
        """

        needed_key = (source_node_name, dest_node_name, lsp_name)

        if needed_key not in (lsp._key for lsp in self.rsvp_lsp_objects):
            msg = ("LSP with source node %s, dest node %s, and name %s "
                   "does not exist in model" % (source_node_name, dest_node_name, lsp_name))
            raise ModelException(msg)
        else:
            for lsp in (lsp for lsp in self.rsvp_lsp_objects):
                if lsp._key == needed_key:
                    return lsp

    # Interface calls
    def get_interface_object(self, interface_name, node_name):
        """Returns an interface object for specified node name and interface name"""

        self._does_interface_exist(interface_name, node_name)

        node_object = self.get_node_object(node_name)

        int_object = [interface for interface in node_object.interfaces(self) if interface.name == interface_name]
        return int_object[0]

    def _does_interface_exist(self, interface_name, node_object_name):
        int_key = (interface_name, node_object_name)
        interface_key_iterator = (interface._key for interface in
                                  self.interface_objects)

        if int_key not in (interface_key_iterator):
            raise ModelException('specified interface does not exist')

    def get_circuit_object_from_interface(self, interface_name, node_name):
        """
        Returns a Circuit object, given a Node name and Interface name
        """

        # Does interface exist?
        self._does_interface_exist(interface_name, node_name)

        interface = self.get_interface_object(interface_name, node_name)

        ckts = [ckt for ckt in self.circuit_objects if interface in (ckt.interface_a, ckt.interface_b)]

        return ckts[0]

    # Convenience calls #####
    def get_failed_interface_objects(self):
        """
        Returns a list of all failed interfaces in the Model
        """
        failed_interfaces = []

        for interface in (interface for interface in self.interface_objects):
            if interface.failed:
                failed_interfaces.append(interface)

        return failed_interfaces

    def get_unfailed_interface_objects(self):
        """
        Returns a list of all non-failed interfaces in the Model
        """

        unfailed_interface_objects = set()

        interface_iter = (interface for interface in self.interface_objects)

        for interface in interface_iter:
            if not interface.failed:
                unfailed_interface_objects.add(interface)

        return unfailed_interface_objects

    def get_unrouted_demand_objects(self):
        """
        Returns list of demand objects that cannot be routed
        """
        unrouted_demands = []
        for demand in (demand for demand in self.demand_objects):
            if demand.path == "Unrouted":
                unrouted_demands.append(demand)

        return unrouted_demands

    def change_interface_name(self, node_name,
                              current_interface_name,
                              new_interface_name):
        """Changes interface name"""
        interface_to_edit = self.get_interface_object(current_interface_name, node_name)
        interface_to_edit.name = new_interface_name

        return interface_to_edit

    def fail_interface(self, interface_name, node_name):
        """Fails the Interface object for the interface_name/node_name pair"""

        # Get the interface object
        interface_object = self.get_interface_object(interface_name, node_name)

        # Does interface exist?
        if interface_object not in self.interface_objects:
            ModelException('specified interface does not exist')

        # find the remote interface
        remote_interface_object = interface_object.get_remote_interface(self)

        remote_interface_object.failed = True
        interface_object.failed = True

    def unfail_interface(self, interface_name, node_name, raise_exception=False):
        """
        Unfails the Interface object for the interface_name, node_name pair.
        :param interface_name:
        :param node_name:
        :param raise_exception: If raise_excecption=True, an exception
                                will be raised if the interface cannot be unfailed.
                                An example of this would be if you tried to unfail
                                the interface when the parent node or remote node
                                was in a failed state
        :return: Interface object from Model that is not failed
        """

        if not (isinstance(raise_exception, bool)):
            message = "raise_exception must be boolean value"
            raise ModelException(message)

        # Get the interface object
        interface_object = self.get_interface_object(interface_name, node_name)

        # Does interface exist?
        if interface_object not in set(self.interface_objects):
            ModelException('specified interface does not exist')

        # Find the remote interface
        remote_interface = interface_object.get_remote_interface(self)

        # Ensure local and remote nodes are failed == False and set reservable
        # bandwidth on each interface to interface.capacity
        if self.get_node_object(interface_object.node_object.name).failed is False and \
                self.get_node_object(remote_interface.node_object.name).failed is False:

            remote_interface.failed = False
            remote_interface.reserved_bandwidth = 0
            interface_object.failed = False
            interface_object.reserved_bandwidth = 0
            self.validate_model()
        else:
            if raise_exception:
                message = ("Local and/or remote node are failed; cannot have "
                           "unfailed interface on failed node.")
                raise ModelException(message)

    def get_all_paths_reservable_bw(self, source_node_name, dest_node_name, include_failed_circuits=True,
                                    cutoff=10, needed_bw=0):
        """
        For a source and dest node name pair, find all simple path(s) with at
        least needed_bw reservable bandwidth available less than or equal to
        cutoff hops long.
        The amount of simple paths (paths that don't have repeating nodes) can
        be very large for larger topologies and so this call can be very expensive.
        Use the cutoff argument to limit the path length to consider to cut down on
        the time it takes to run this call.
        :param source_node_name: name of source node in path
        :param dest_node_name: name of destination node in path
        :param include_failed_circuits: include failed circuits in the topology
        :param needed_bw: the amount of reservable bandwidth required on the path
        :param cutoff: max amount of path hops
        :return: Return the path(s) in dictionary form:
                 path = {'path': [list of all path routes]}
        """

        # Define a networkx DiGraph to find the path
        G = self._make_weighted_network_graph(include_failed_circuits=include_failed_circuits, needed_bw=needed_bw)

        # Define the Model-style path to be built
        converted_path = dict()
        converted_path['path'] = []

        # Find the simple paths in G between source and dest
        digraph_all_paths = nx.all_simple_paths(G, source_node_name, dest_node_name, cutoff=cutoff)

        try:
            for path in digraph_all_paths:
                model_path = self._convert_nx_path_to_model_path(path)
                converted_path['path'].append(model_path)
            return converted_path
        except BaseException:
            return converted_path

    def get_shortest_path(self, source_node_name, dest_node_name, needed_bw=0):
        """
        For a source and dest node name pair, find the shortest path(s) with at
        least needed_bw available.
        :param source_node_name: name of source node in path
        :param dest_node_name: name of destination node in path
        :param needed_bw: the amount of reservable bandwidth required on the path
        :return: Return the shortest path in dictionary form:
                 shortest_path = {'path': [list of shortest path routes], 'cost': path_cost}
        """

        # Define a networkx DiGraph to find the path
        G = self._make_weighted_network_graph(include_failed_circuits=False, needed_bw=needed_bw)

        # Define the Model-style path to be built
        converted_path = dict()
        converted_path['path'] = []
        converted_path['cost'] = None

        # Find the shortest paths in G between source and dest
        digraph_shortest_paths = nx.all_shortest_paths(G, source_node_name,
                                                       dest_node_name,
                                                       weight='cost')

        try:
            for path in digraph_shortest_paths:
                model_path = self._convert_nx_path_to_model_path(path)
                converted_path['path'].append(model_path)
                converted_path['cost'] = nx.shortest_path_length(G, source_node_name, dest_node_name, weight='cost')
            return converted_path
        except BaseException:
            return converted_path

    def get_shortest_path_for_routed_lsp(self, source_node_name, dest_node_name, lsp, needed_bw):
        """
        For a source and dest node name pair, find the shortest path(s) with at
        least needed_bw available for an LSP that is already routed.
        Return the shortest path in dictionary form:
        shortest_path = {'path': [list of shortest path routes], 'cost': path_cost}
        """

        # Define a networkx DiGraph to find the path
        G = self._make_weighted_network_graph_routed_lsp(lsp, needed_bw=needed_bw)

        # Define the Model-style path to be built
        converted_path = dict()
        converted_path['path'] = []
        converted_path['cost'] = None

        # Find the shortest paths in G between source and dest
        digraph_shortest_paths = nx.all_shortest_paths(G, source_node_name,
                                                       dest_node_name,
                                                       weight='cost')
        try:
            for path in digraph_shortest_paths:
                model_path = self._convert_nx_path_to_model_path(path)
                converted_path['path'].append(model_path)
                converted_path['cost'] = nx.shortest_path_length(G, source_node_name,
                                                                 dest_node_name, weight='cost')
            return converted_path
        except BaseException:
            return converted_path

    def _convert_nx_path_to_model_path(self, nx_graph_path):
        """Given a path from an networkx DiGraph, converts that
        path to a Model style path and returns that Model style path
        A networkx path is a list of nodes in order of transit.
        ex: ['A', 'B', 'G', 'D', 'F']
        The corresponding model style path would be:
        [Interface(name = 'A-to-B', cost = 20, capacity = 125, node_object = Node('A'),
            remote_node_object = Node('B'), circuit_id = 9),
        Interface(name = 'B-to-G', cost = 10, capacity = 100, node_object = Node('B'),
            remote_node_object = Node('G'), circuit_id = 6),
        Interface(name = 'G-to-D', cost = 10, capacity = 100, node_object = Node('G'),
            remote_node_object = Node('D'), circuit_id = 2),
        Interface(name = 'D-to-F', cost = 10, capacity = 300, node_object = Node('D'),
            remote_node_object = Node('F'), circuit_id = 1)]
        """

        # Define a model-style path to build
        model_path = []
        # look at each hop in the path
        for hop in nx_graph_path:
            current_hop_index = nx_graph_path.index(hop)
            next_hop_index = current_hop_index + 1
            if next_hop_index < len(nx_graph_path):
                next_hop = nx_graph_path[next_hop_index]
                interface = self.get_interface_object_from_nodes(hop, next_hop)
                model_path.append(interface)

        return model_path

    # NODE CALLS ######
    def get_node_interfaces(self, node_name):
        """Returns list of interfaces on specified node name"""
        return Node(node_name).interfaces(self)

    def fail_node(self, node_name):
        """Fails specified node"""

        # Find node's interfaces and fail them
        ints_to_fail_iterator = (interface for interface in
                                 self.get_node_interfaces(node_name))

        for interface in ints_to_fail_iterator:
            self.fail_interface(interface.name, node_name)

        # Change the failed property on the specified node
        self.get_node_object(node_name).failed = True

    def unfail_node(self, node_name):
        """Unfails the Node with name=node_name"""

        # Change the failed property on the specified node;
        self.get_node_object(node_name).failed = False

        # Find node's interfaces and unfail them
        ints_to_unfail_iterator = (interface for interface in self.get_node_interfaces(node_name))

        for interface in ints_to_unfail_iterator:

            # Unfail the interfaces if the remote node is not failed
            if not interface.remote_node_object.failed:
                # Unfail the specific interface
                self.unfail_interface(interface.name, node_name, False)

                # Unfail the remote interface
                remote_int = interface.get_remote_interface(self)
                self.unfail_interface(remote_int.name,
                                      remote_int.node_object.name, False)

    def get_failed_node_objects(self):
        """
        Returns a list of all failed nodes
        """
        failed_nodes = []

        for node in (node for node in self.node_objects):
            if node.failed:
                node_object = self.get_node_object(node.name)
                failed_nodes.append(node_object)

        return failed_nodes

    def get_non_failed_node_objects(self):
        """Returns a list of all failed nodes"""
        non_failed_nodes = []

        for node in (node for node in self.node_objects):
            if not node.failed:
                node_object = self.get_node_object(node.name)
                non_failed_nodes.append(node_object)

        return non_failed_nodes

    # Display calls #########
    def display_interface_status(self):  # pragma: no cover
        """Returns failed = True/False for each interface"""

        print('Node'.ljust(12), 'Interface'.ljust(12), 'Remote Node'.ljust(12), end=' ')
        print('Failed'.ljust(12))

        interface_iterator = (interface for interface in self.interface_objects)

        for interface in interface_iterator:
            print(interface.node_object.name.ljust(12), interface.name.ljust(12), end=' ')
            print(interface.remote_node_object.name.ljust(12), end=' ')
            print(str(interface.failed).ljust(12))

    def display_node_status(self):  # pragma: no cover
        """Returns failed = True/False for each node"""

        print('Node'.ljust(12), 'Failed'.ljust(12))

        node_iterator = (node for node in self.node_objects)

        for node in node_iterator:
            print(node.name.ljust(12), str(node.failed).ljust(12))

    def display_interfaces_traffic(self):  # pragma: no cover
        """
        A human-readable(-ish) display of interfaces and traffic on each
        """

        print('Node'.ljust(12), 'Interface'.ljust(12), 'Remote Node'.ljust(12), 'Traffic'.ljust(12))

        interface_iterator = (interface for interface in self.interface_objects)

        for interface in interface_iterator:
            print(interface.node_object.name.ljust(12), interface.name.ljust(12), end=' ')
            print(interface.remote_node_object.name.ljust(12), end=' ')
            print(repr(interface.traffic).ljust(12))

    def display_demand_paths(self):  # pragma: no cover
        """
        Displays each demand and its path(s) across the network
        """

        demand_iter = (demand for demand in self.demand_objects)

        for demand in demand_iter:
            print('demand._key is', demand._key)
            print('Demand has %s paths:' % (len(demand.path)))
            for path in demand.path:
                pprint(path)
                print()
            print()
            print()

    def display_interface_objects(self):  # pragma: no cover
        """Displays interface objects in a more human readable manner"""

        for interface in self.interface_objects:
            pprint(interface)
            print()

    def _make_weighted_network_graph(self, include_failed_circuits=True, needed_bw=0, rsvp_required=False):
        """
        Returns a networkx weighted networkx digraph from
        the input Model object

        :param include_failed_circuits: include interfaces from currently failed
        circuits in the graph?
        :param needed_bw: how much reservable_bandwidth is required?
        :param rsvp_required: True|False; only consider rsvp_enabled interfaces?

        :return: networkx digraph with edges that conform to the needed_bw and
        rsvp_required parameters
        """

        G = nx.DiGraph()

        # Get all the edges that meet 'failed' and 'reservable_bw' criteria
        if include_failed_circuits is False:
            considered_interfaces = (interface for interface in self.interface_objects
                                     if (interface.failed is False and
                                         interface.reservable_bandwidth >= needed_bw))
        elif include_failed_circuits is True:
            considered_interfaces = (interface for interface in self.interface_objects
                                     if interface.reservable_bandwidth >= needed_bw)

        if rsvp_required is True:
            edge_names = ((interface.node_object.name,
                           interface.remote_node_object.name, interface.cost)
                          for interface in considered_interfaces
                          if interface.rsvp_enabled is True)
        else:
            edge_names = ((interface.node_object.name,
                           interface.remote_node_object.name, interface.cost)
                          for interface in considered_interfaces)

        # Add edges to networkx DiGraph
        G.add_weighted_edges_from(edge_names, weight='cost')

        # Add all the nodes
        node_name_iterator = (node.name for node in self.node_objects)
        G.add_nodes_from(node_name_iterator)

        return G

    def _make_weighted_network_graph_routed_lsp(self, lsp, needed_bw=0):
        """
        Looks for a new path with needed_bw reservable bandwidth for an RSVP LSP
        that is currently routed.
        Returns a networkx weighted network directional graph from the input Model object.
        Considers edges with needed_bw of reservable_bandwidth and also takes into account
        reserved_bandwidth by the lsp on Interfaces in the existing LSP path
        :param lsp: RSVP LSP that is currently routed
        :param needed_bw: how much bandwidth is needed for the RSVP LSP's new path
        :return: networkx DiGraph with eligible edges
        """
        G = nx.DiGraph()

        # The Interfaces that the lsp is routed over currently
        lsp_path_interfaces = lsp.path['interfaces']

        # Since this is for a routed LSP, rsvp_enabled must be True and interface must
        # not be failed
        eligible_interface_generator = (interface for interface in self.interface_objects if
                                        interface.failed is False and interface.rsvp_enabled is True)

        eligible_interfaces = set()

        # Find only the interfaces that are not failed and that have
        # enough reservable_bandwidth
        for interface in eligible_interface_generator:
            # Add back the lsp's reserved bandwidth to Interfaces already in its path
            if interface in lsp_path_interfaces:
                effective_reservable_bw = interface.reservable_bandwidth + lsp.reserved_bandwidth
            else:
                effective_reservable_bw = interface.reservable_bandwidth

            if effective_reservable_bw >= needed_bw:
                eligible_interfaces.add(interface)

        # Get edge names in eligible_interfaces
        edge_names = ((interface.node_object.name,
                       interface.remote_node_object.name, interface.cost)
                      for interface in eligible_interfaces)

        # Add edges to networkx DiGraph
        G.add_weighted_edges_from(edge_names, weight='cost')

        # Add all the nodes
        node_name_iterator = (node.name for node in self.node_objects)
        G.add_nodes_from(node_name_iterator)

        return G

    @classmethod
    def load_model_file(cls, data_file):  # TODO - make sure doc strings for this come out well in docs dir
        """
        Opens a network_modeling data file and returns a model containing
        the info in the data file.  The data file must be of the appropriate
        format to produce a valid model.  This cannot be used to open
        multiple models in a single python instance - there may be
        unpredictable results in the info in the models.
        The format for the file must be a tab separated value file.
        This docstring you are reading may not display the table info
        explanations/examples below correctly on https://pyntm.readthedocs.io/en/latest/api.html.
        Recommend either using help(Model.load_model_file) at the python3 cli or
        looking at one of the sample model data_files in github:
        https://github.com/tim-fiola/network_traffic_modeler_py3/blob/master/examples/sample_network_model_file.csv
        https://github.com/tim-fiola/network_traffic_modeler_py3/blob/master/examples/lsp_model_test_file.csv
        The following headers must exist, with the following tab-column
        names beneath:

        INTERFACES_TABLE
        node_object_name - name of node	where interface resides
        remote_node_object_name	- name of remote node
        name - interface name
        cost - IGP cost/metric for interface
        capacity - capacity
        rsvp_enabled (optional) - is interface allowed to carry RSVP LSPs? True|False; default is True
        percent_reservable_bandwidth (optional) - percent of capacity allowed to be reserved by RSVP LSPs; this
        value should be given as a percentage value - ie 80% would be given as 80, NOT .80.  Default is 100

        Note - The existence of Nodes will be inferred from the INTERFACES_TABLE.
        So a Node created from an Interface does not have to appear in the
        NODES_TABLE unless you want to add additional attributes for the Node
        such as latitude/longitude

        NODES_TABLE -
        name - name of node
        lon	- longitude (or y-coordinate)
        lat - latitude (or x-coordinate)

        Note - The NODES_TABLE is present for 2 reasons:
        - to add a Node that has no interfaces
        - and/or to add additional attributes for a Node inferred from
        the INTERFACES_TABLE

        DEMANDS_TABLE
        source - source node name
        dest - destination node name
        traffic	- amount of traffic on demand
        name - name of demand
        RSVP_LSP_TABLE (this table is optional)
        source - source node name
        dest - destination node name
        name - name of LSP
        configured_setup_bw - if LSP has a fixed, static configured setup bandwidth, place that static value here,
        if LSP is auto-bandwidth, then leave this blank for the LSP

        Functional model files can be found in this directory in
        https://github.com/tim-fiola/network_traffic_modeler_py3/tree/master/examples
        Here is an example of a data file:

        INTERFACES_TABLE
        node_object_name	remote_node_object_name	name	cost	capacity    rsvp_enabled    percent_reservable_bandwidth  # noqa E501
        A	B	A-to-B	4	100
        B	A	B-to-A	4	100
        NODES_TABLE
        name	lon	lat
        A	50	0
        B	0	-50
        DEMANDS_TABLE
        source	dest	traffic	name
        A	B	80	dmd_a_b_1
        RSVP_LSP_TABLE
        source	dest	name    configured_setup_bw
        A	B	lsp_a_b_1   10
        A	B	lsp_a_b_2

        :param data_file: file with model info
        :return: Model object
        """
        # TODO - allow user to add user-defined columns in NODES_TABLE and add that as an attribute to the Node
        # TODO - add support for SRLGs

        interface_set = set()
        node_set = set()
        demand_set = set()
        lsp_set = set()

        # Open the file with the data, read it, and split it into lines
        with open(data_file, 'r') as f:
            data = f.read()

        lines = data.splitlines()

        # Define the Interfaces from the data and extract the presence of
        # Nodes from the Interface data
        int_info_begin_index = 2
        int_info_end_index = find_end_index(int_info_begin_index, lines)
        interface_set, node_set = cls._extract_interface_data_and_implied_nodes(int_info_begin_index,
                                                                                int_info_end_index, lines)

        # Define the explicit nodes info from the file
        nodes_info_begin_index = int_info_end_index + 3
        nodes_info_end_index = find_end_index(nodes_info_begin_index, lines)
        node_lines = lines[nodes_info_begin_index:nodes_info_end_index]
        for node_line in node_lines:
            cls._add_node_from_data(demand_set, interface_set, lines, lsp_set, node_line, node_set)

        # Define the demands info
        demands_info_begin_index = nodes_info_end_index + 3
        demands_info_end_index = find_end_index(demands_info_begin_index, lines)
        # There may or may not be LSPs in the model, so if there are not,
        # set the demands_info_end_index as the last line in the file
        if not demands_info_end_index:
            demands_info_end_index = len(lines)

        demands_lines = lines[demands_info_begin_index:demands_info_end_index]

        for demand_line in demands_lines:
            try:
                cls._add_demand_from_data(demand_line, demand_set, lines, node_set)
            except ModelException as e:
                err_msg = e.args[0]
                raise ModelException(err_msg)

        # Define the LSP info (if present)
        # If the demands_info_end_index is the same as the length of the
        # lines list, then there is no LSP section
        if demands_info_end_index != len(lines):
            try:
                cls._add_lsp_from_data(demands_info_end_index, lines, lsp_set, node_set)
            except ModelException as e:
                err_msg = e.args[0]
                raise ModelException(err_msg)

        return cls(interface_set, node_set, demand_set, lsp_set)

    @classmethod
    def _extract_interface_data_and_implied_nodes(cls, int_info_begin_index, int_info_end_index, lines):
        """
        Extracts interface data from lines and adds Interface objects to a set.
        Also extracts the implied Nodes from the Interfaces and adds those Nodes to a set.
        :param int_info_begin_index: Index position in lines where interface info begins
        :param int_info_end_index:  Index position in lines where interface info ends
        :param lines: lines of data describing a Model objects
        :return: set of Interface objects, set of Node objects created from lines
        """

        interface_set = set()
        node_set = set()
        interface_lines = lines[int_info_begin_index:int_info_end_index]
        # Add the Interfaces to a set
        for interface_line in interface_lines:
            # Read interface characteristics
            if len(interface_line.split()) == 5:
                node_name, remote_node_name, name, cost, capacity = interface_line.split()
                rsvp_enabled_bool = True
                percent_reservable_bandwidth = 100
            elif len(interface_line.split()) == 6:
                node_name, remote_node_name, name, cost, capacity, rsvp_enabled = interface_line.split()
                if rsvp_enabled in [True, 'T', 'True', 'true']:
                    rsvp_enabled_bool = True
                else:
                    rsvp_enabled_bool = False
                percent_reservable_bandwidth = 100
            elif len(interface_line.split()) >= 7:
                node_name, remote_node_name, name, cost, capacity, \
                    rsvp_enabled, percent_reservable_bandwidth = interface_line.split()
                if rsvp_enabled in [True, 'T', 'True', 'true']:
                    rsvp_enabled_bool = True
                else:
                    rsvp_enabled_bool = False
            else:
                msg = ("node_name, remote_node_name, name, cost, and capacity "
                       "must be defined for line {}, line index {}".format(interface_line,
                                                                           lines.index(interface_line)))
                raise ModelException(msg)

            new_interface = Interface(name, int(cost), float(capacity), Node(node_name), Node(remote_node_name),
                                      None, rsvp_enabled_bool, float(percent_reservable_bandwidth))

            if new_interface._key not in set([interface._key for interface in interface_set]):
                interface_set.add(new_interface)
            else:
                print("{} already exists in model; disregarding line {}".format(new_interface,
                                                                                lines.index(interface_line)))

            # Derive Nodes from the Interface data
            if node_name not in set([node.name for node in node_set]):
                node_set.add(new_interface.node_object)
            if remote_node_name not in set([node.name for node in node_set]):
                node_set.add(new_interface.remote_node_object)

        return interface_set, node_set

    def get_demand_objects_source_node(self, source_node_name):
        """
        Returns list of demand objects originating at the source node
        """

        demand_list = []
        for demand in (demand for demand in self.demand_objects):
            if demand.source_node_object.name == source_node_name:
                demand_list.append(demand)

        return demand_list

    def get_demand_objects_dest_node(self, dest_node_name):
        """Returns list of demands objects originating at the
        destination node """
        demand_list = []
        for demand in (demand for demand in self.demand_objects):
            if demand.dest_node_object.name == dest_node_name:
                demand_list.append(demand)

        return demand_list

    # ### SRLG Calls ### #
    def get_srlg_object(self, srlg_name, raise_exception=True):
        """
        Returns SRLG in self with srlg_name
        :param srlg_name: name of SRLG
        :param raise_exception: raise an exception if SRLG with name=srlg_name does not
        exist in self
        :return: None
        """

        srlg_already_in_model = [srlg for srlg in self.srlg_objects if srlg.name == srlg_name]

        if len(srlg_already_in_model) == 1:
            return srlg_already_in_model[0]  # There will only be one SRLG with srlg_name
        else:
            if raise_exception:
                msg = "No SRLG with name {} exists in Model".format(srlg_name)
                raise ModelException(msg)
            else:
                return None

    def fail_srlg(self, srlg_name):
        """
        Sets SRLG with name srlg_name to failed = True
        :param srlg_name: name of SRLG to fail
        :return: none
        """

        srlg_to_fail = self.get_srlg_object(srlg_name)

        # Find SRLG's Nodes to fail
        nodes_to_fail_iterator = (node for node in self.node_objects if node in srlg_to_fail.node_objects)

        for node in nodes_to_fail_iterator:
            self.fail_node(node.name)

        # Find SRLG's Interfaces to fail
        interfaces_to_fail_iterator = (interface for interface in self.interface_objects if
                                       interface in srlg_to_fail.interface_objects)

        for interface in interfaces_to_fail_iterator:
            self.fail_interface(interface.name, interface.node_object.name)

        # Change the failed property on the specified srlg
        srlg_to_fail.failed = True

    def unfail_srlg(self, srlg_name):
        """
        Sets SRLG with srlg_name to failed = False
        :param srlg_name: name of SRLG to unfail
        :return: none
        """

        srlg_to_unfail = self.get_srlg_object(srlg_name)

        # Change the failed property on the specified srlg
        srlg_to_unfail.failed = False

        # Find SRLG's Nodes to unfail
        nodes_to_unfail_iterator = (node for node in self.node_objects if node in srlg_to_unfail.node_objects)

        # Node will stay failed if it's part of another SRLG that is still failed;
        # in that case, the unfail_node will create an exception; ignore that exception
        for node in nodes_to_unfail_iterator:
            try:
                self.unfail_node(node.name)
            except ModelException:
                pass

        # Find SRLG's Interfaces to unfail
        interfaces_to_unfail_iterator = (interface for interface in self.interface_objects if
                                         interface in srlg_to_unfail.interface_objects)

        # Interface will stay failed if it's part of another SRLG that is still failed or
        # if the local/remote Node is failed;  in that case, the unfail_interface
        # will create an exception; ignore that exception
        for interface in interfaces_to_unfail_iterator:
            try:
                self.unfail_interface(interface.name, interface.node_object.name)
            except ModelException:
                pass

    def add_srlg(self, srlg_name):
        """
        Adds SRLG object to Model
        :param srlg_name: name of SRLG
        :return:
        """

        if srlg_name in set([srlg.name for srlg in self.srlg_objects]):
            raise ModelException("SRLG with name {} already exists in Model".format(srlg_name))
        else:
            srlg = SRLG(srlg_name, self)
            self.srlg_objects.add(srlg)

    def multiple_links_between_nodes(self):
        """
        Ensures there is no more than a single interface facing a
        given remote node (that there are no parallel interfaces
        between nodes)
        :return: a list of parallel interfaces; if
        there are no parallel interfaces, the list is empty
        """

        connected_nodes_list = [(interface.node_object.name + '-' + interface.remote_node_object.name) for interface
                                in self.interface_objects]

        connected_nodes_set = set(connected_nodes_list)

        # If there are parallel links between nodes, create a list of the
        # parallel links, sort it, and return the list
        if len(connected_nodes_list) != len(connected_nodes_set):
            parallel_links = [connection for connection in connected_nodes_list if
                              connected_nodes_list.count(connection) > 1]
            parallel_links.sort()

            return parallel_links

        else:
            return []
