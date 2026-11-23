import copy,json,unittest
from pathlib import Path
from micro_objects import process

class ObjectTests(unittest.TestCase):
    def setUp(self):self.d=json.loads(Path('fixtures/input.json').read_text())
    def test_ring_geometry_and_intensity(self):
        r=process(self.d);o=r['objects'][0]
        self.assertEqual((o['pixel_count'],o['area'],o['edge_perimeter'],o['holes']),(8,'16','24',1))
        self.assertEqual(o['centroid'],['5','5/2']);self.assertEqual(o['mean_intensity'],'22');self.assertEqual(o['intensity_sum'],176)
        self.assertEqual(r['summary'],{'objects':2,'accepted':1,'foreground_pixels':9,'selected_pixels':8})
    def test_selection_preserves_source_labels(self):
        r=process(self.d);self.assertEqual(r['labels'][4][6],2);self.assertEqual(r['selected_labels'][4][6],0)
        self.assertEqual(r['objects'][1]['rejection_reasons'],['below_min_pixels','touches_border'])
        self.d['selection'].update(min_pixels=1,exclude_border=False);self.assertEqual(process(self.d)['summary']['accepted'],2)
    def test_diagonal_connectivity(self):
        self.d.update(mask=['10','01'],intensity=[[1,0],[0,3]])
        self.assertEqual(process(self.d)['summary']['objects'],2)
        self.d['connectivity']=8;r=process(self.d);self.assertEqual(r['summary']['objects'],1);self.assertEqual(r['objects'][0]['mean_intensity'],'2')
    def test_complementary_topology(self):
        self.d.update(mask=['111','101','110'],intensity=[[0]*3 for _ in range(3)])
        self.assertEqual(process(self.d)['objects'][0]['holes'],0)
        self.d['connectivity']=8;self.assertEqual(process(self.d)['objects'][0]['holes'],1)
    def test_empty_foreground(self):
        self.d['mask']=['0000000']*5;r=process(self.d);self.assertEqual(r['objects'],[]);self.assertEqual(r['summary']['foreground_pixels'],0)
    def test_bad_shape_and_size(self):
        for mutate in (lambda d:d['intensity'].pop(),lambda d:d.update(pixel_size=['0','1']),lambda d:d.update(connectivity=5),lambda d:d['intensity'][0].__setitem__(0,True)):
            d=copy.deepcopy(self.d);mutate(d)
            with self.assertRaises(ValueError):process(d)
