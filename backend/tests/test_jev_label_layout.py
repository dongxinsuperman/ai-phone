from copy import deepcopy
from ai_phone.agent.trajectory_cache.jev_click.label_layout import place_labels, intersects


def test_nested_bottom_badges_are_visible_without_changing_candidates():
    nodes=[{'id':'N30','bounds':[0,1788,225,1983]},
           {'id':'N31','bounds':[0,1794,1125,2001]},
           {'id':'N32','bounds':[225,1788,450,1983]},
           {'id':'N34','bounds':[450,1788,675,1983]},
           {'id':'N37','bounds':[675,1794,900,1989]},
           {'id':'N38','bounds':[900,1788,1125,1983]}]
    original=deepcopy(nodes)
    result=place_labels(nodes,{n['id']:(42,24) for n in nodes},(1125,2001))
    assert nodes==original
    assert result['N30'][:2]==(0,1788)
    assert result['N31'][:2]!=(0,1794)
    for i,a in enumerate(result.values()):
        assert 0<=a[0]<a[2]<=1125 and 0<=a[1]<a[3]<=2001
        for b in list(result.values())[i+1:]:assert not intersects(a,b)
    assert result==place_labels(nodes,{n['id']:(42,24) for n in nodes},(1125,2001))
