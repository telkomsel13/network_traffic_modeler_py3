"A Class to represent Shared Risk Link Groups (SRLGs) in a Model"


class SRLG(object):
    """
    Represents a collection of Model objects with shared risk factors.
    Can include:
    Nodes
    Circuits

    The name

    """

    def __init__(self, name, circuit_objects=set(), node_objects=set()):
        self.circuit_objects = circuit_objects
        self.node_objects = node_objects
        self.name = name

    def __repr__(self):
        return "SRLG(Name: {}, Circuits: {}, Nodes: {})".format(self.name,
                                                                len(self.circuit_objects),
                                                                len(self.node_objects))

    def interfaces(self, model):
        """
        Returns set of Interface objects, derived from Circuits in the SRLG
        :param model: Model object
        :return: set of Interface objects
        """
        interfaces = set()
        for ckt in (ckt for ckt in self.circuit_objects):
            ints = ckt.get_circuit_interfaces(model)
            interfaces.add(ints[0])
            interfaces.add(ints[1])

        return interfaces
