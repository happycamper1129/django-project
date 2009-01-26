# Valid expression extensions.
VALID_FILTERS = set(['exact', 'gt', 'gte', 'lt', 'lte', 'in'])
FILTER_SEPARATOR = '__'

# The maximum number of items to display in a SearchQuerySet.__repr__
REPR_OUTPUT_SIZE = 20

# Number of SearchResults to load at a time.
ITERATOR_LOAD_PER_QUERY = 20
