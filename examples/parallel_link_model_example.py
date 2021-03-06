import sys
sys.path.append('../')

from pprint import pprint

from pyNTM import Parallel_Link_Model

# Make the Parallel_Link_Model
model = Parallel_Link_Model.load_model_file('model_test_topology_multidigraph.csv')
model.update_simulation()
print()
# Get shorteset path from Node A to Node D
a_d = model.get_shortest_path('A', 'D')

# Print the shortest path(s)
print("There are {} shortest path(s) from Node A to Node D.".format(len(a_d['path'])))
print("The shortest path(s) from Node A to Node D:")
for path in a_d['path']:
    pprint(path)
    print()

# Find the shortest path(s) from A to D with 75 units of reservable_bandwidth
a_d_75 = model.get_shortest_path('A', 'D', needed_bw=75)

# Print the path(s)
print("There are {} shortest path(s) from Node A to Node D with at least 75 units of"
      "reserable bandwidth.".format(len(a_d_75['path'])))
print("The shortest path(s) from Node A to Node D with 75 units of reservable bandwidth:")
for path in a_d_75['path']:
    pprint(path)
    print()

print()

# Find ALL paths (not just shortest paths) from A to D over
# interfaces that are not failed, consisting of 3 hops or less,
# with at least 80 units of reservable bandwidth
all_paths = model.get_all_paths_reservable_bw('A', 'D', False, 3, 80)

print("All {} paths from A to D over interfaces that are not failed, consisting of 3 hops or less, with"
      "at least 80 units of reservable bandwidth:".format(len(all_paths['path'])))
for path in all_paths['path']:
    pprint(path)
    print()


