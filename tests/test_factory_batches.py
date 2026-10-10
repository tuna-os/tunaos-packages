"""Complete factory selections must fit without dropping continuation work.

Falsification: fill all three shards then add native continuations; the complete
inventory must remain visible in explicit deterministic batches, never truncated.
"""
import importlib.util
import pytest
from pathlib import Path

ROOT=Path(__file__).parents[1]
SPEC=importlib.util.spec_from_file_location('batch_planner',ROOT/'scripts/plan-package-factory.py')
planner=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)


def cell(number,native=False):
    return {'id':f'cell-{number:04}', 'engine':'build-chain' if native else 'tideforge', 'tiers':'','canary':False}


def test_small_selection_preserves_existing_matrix_order():
    cells=[cell(index,index==5) for index in range(250)]
    plan=planner.selection_batches(cells,'a'*40)
    assert plan['shards']==[[cells[:200],cells[200:],[]]]


def test_all_cells_and_continuations_fit_without_silent_drop():
    cells=[cell(index,index<24) for index in range(598)]
    plan=planner.selection_batches(cells,'a'*40)
    assert len(plan['shards'])==2
    assert [sum(map(len,batch)) for batch in plan['shards']]==[552,46]
    seen=[]
    for batch in plan['shards']:
        native=[value for value in batch[0] if value['engine']=='build-chain']
        assert len(batch[0])<=200
        assert len(batch[1])+len(native)<=200
        assert len(batch[2])+len(native)<=200
        assert not any(value['engine']=='build-chain' for shard in batch[1:] for value in shard)
        seen.extend(value['id'] for shard in batch for value in shard)
    assert sorted(seen)==sorted(value['id'] for value in cells)
    assert len(seen)==len(set(seen))
    assert [batch['cells'] for batch in plan['planned_batches']]==[
        [value['id'] for shard in batch for value in shard] for batch in plan['shards']]


def test_large_native_selection_reserves_all_continuations():
    cells=[cell(index,True) for index in range(201)]
    # The old selection fits initial shards but loses continuation for native
    # cells outside shard0. Preserve small-fit shape as requested; explicit
    # overflow fixture ensures the batching path handles >200 native safely.
    cells += [cell(index) for index in range(201,801)]
    plan=planner.selection_batches(cells,'a'*40)
    assert sum(sum(map(len,batch)) for batch in plan['shards'])==801
    for batch in plan['shards']:
        native=sum(value['engine']=='build-chain' for value in batch[0])
        assert all(len(shard)+native<=200 for shard in batch[1:])


def test_selection_identity_binds_source_and_full_inventory():
    cells=[cell(1)]
    first=planner.selection_batches(cells,'a'*40)
    assert first==planner.selection_batches(cells,'a'*40)
    assert first['selection_digest']!=planner.selection_batches(cells,'b'*40)['selection_digest']
    assert first['selection_digest']!=planner.selection_batches([cell(2)],'a'*40)['selection_digest']


def test_empty_selection_retains_three_empty_matrices():
    assert planner.selection_batches([],'a'*40)['shards']==[[[],[],[]]]


def test_duplicate_cells_and_floating_source_rejected():
    with pytest.raises(ValueError):
        planner.selection_batches([cell(1),cell(1)],'a'*40)
    with pytest.raises(ValueError):
        planner.selection_batches([cell(1)],'main')
